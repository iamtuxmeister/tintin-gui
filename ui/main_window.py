"""
MainWindow — the top-level application window.

Layout:
┌──────────────────────────────────────────┐
│  Menu bar                                │
├──────────────────┬───────────────────────┤
│                  │                       │
│  OutputWidget    │  MapWidget            │
│  (split scroll)  │  (graphical map)      │
│                  │                       │
├──────────────────┴───────────────────────┤
│  InputBar  (QLineEdit + Send button)     │
├──────────────────────────────────────────┤
│  ButtonBar  (macro buttons)              │
└──────────────────────────────────────────┘

The horizontal splitter between OutputWidget and MapWidget is user-draggable.
"""

import os
import re as _re   # FIX 1: single module-level import, not repeated in hot path
import sys
import collections as _collections

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSplitter, QLineEdit, QPushButton, QStatusBar,
    QLabel, QFileDialog, QApplication,
)
from PyQt6.QtCore  import Qt, QTimer, QObject, QEvent
from PyQt6.QtGui   import QFont, QFontMetrics, QKeyEvent, QAction, QIcon

from core.tintin_process_compat import TinTinProcess
from core.map_parser        import MapGraph, try_parse_gmcp_line
from core.tt_config_sync    import (
    TinTinConfigLoader, config_file_path,
    parse_tin_file, write_config_file,
)
from ui.output_widget    import OutputWidget
from ui.map_widget       import MapWidget
from ui.right_panel      import RightPanel
from ui.button_bar       import ButtonBar
from ui.session_manager  import SessionManager, Session, _load_sessions, _save_sessions
from ui.config_dialog    import ConfigDialog


class _TabCompleter:
    """
    Collects words from MUD output for tab-completion.

    - Only stores words > 5 chars, lowercased.
    - Tracks which "line number" each word was last seen on.
    - Words not seen in the last WINDOW lines are evicted.
    - Uses an OrderedDict so iteration is insertion-order (recency).
    """
    WINDOW     = 500    # lines before a word is forgotten
    MIN_LEN    = 5      # minimum word length (exclusive, so >5 = 6+)
    _WORD_RE   = _re.compile(r"[a-zA-Z][a-zA-Z-]{5,}")   # 6+ chars, letters and dash only

    def __init__(self):
        # word -> line_number of last sighting
        self._words: dict[str, int] = _collections.OrderedDict()
        self._line  = 0

    def feed(self, text: str):
        """Call with each new chunk of plain (stripped) MUD text."""
        self._line += text.count('\n')
        for w in self._WORD_RE.findall(text):
            lw = w.lower()
            # Move to end (most-recent) on re-insertion
            self._words.pop(lw, None)
            self._words[lw] = self._line
        self._evict()

    def _evict(self):
        cutoff = self._line - self.WINDOW
        # OrderedDict is oldest-first so we can break early
        to_del = [w for w, ln in self._words.items() if ln < cutoff]
        for w in to_del:
            del self._words[w]

    def complete(self, prefix: str) -> list[str]:
        """Return all known words that start with prefix (case-insensitive)."""
        p = prefix.lower()
        return [w for w in self._words if w.startswith(p)]

    def __len__(self):
        return len(self._words)


class _InputLineEdit(QLineEdit):
    """
    QLineEdit with full MUD-client input features:

    History (Up / Down):
      - If the input box is empty, Up/Down scrolls through all history.
      - If the input box has text (and nothing is selected, i.e. you typed
        a partial command), Up searches BACKWARDS through history for the
        nearest entry that STARTS WITH that prefix.  Down searches forward.
        This lets you type "k" and Up through all commands beginning with k.
      - When a history entry is recalled it is fully selected so you can
        press Enter to resend immediately or type to replace it.

    Left / Right:
      - Left  → jump to position 0  (Home), cursor visible, text kept.
      - Right → jump to end of line, text kept.
      - With Shift or Ctrl the default QLineEdit behaviour applies.

    Tab completion:
      - Tab completes the word currently under/before the cursor against
        the _TabCompleter word list built from MUD output.
      - Repeated Tab cycles through all matches.
      - Any non-modifier keypress resets the cycle.
    """

    def __init__(self, completer: _TabCompleter, parent=None):
        super().__init__(parent)
        self._history:      list[str] = []
        self._hist_idx:     int       = -1
        self._hist_prefix:  str       = ""   # prefix locked when Up first pressed
        self._prefix_search: bool     = False
        self._completer:    _TabCompleter = completer
        self._tab_matches:  list[str] = []
        self._tab_idx:      int       = -1
        self._tab_prefix:   str       = ""
        self._tab_anchor:   int       = 0

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def add_history(self, text: str):
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._hist_idx = -1
        self._prefix_search = False
        self._hist_prefix   = ""
        self._clear_tab_state()

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent):
        key  = event.key()
        mods = event.modifiers()

        # ── History search (Up / Down) ──────────────────────────────────
        if key == Qt.Key.Key_Up:
            self._history_step(-1)
            self._clear_tab_state()
            return

        if key == Qt.Key.Key_Down:
            self._history_step(+1)
            self._clear_tab_state()
            return

        # ── Home / End via bare Left / Right ───────────────────────────
        # If text is selected: Left → start, Right → end (collapse selection)
        # If no selection: behave like a normal input (move one character)
        if key == Qt.Key.Key_Left and not mods:
            if self.hasSelectedText():
                self.setCursorPosition(0)
                self._reset_history_state()
                self._clear_tab_state()
                return
            # else fall through to super() for normal single-char movement

        if key == Qt.Key.Key_Right and not mods:
            if self.hasSelectedText():
                self.setCursorPosition(len(self.text()))
                self._reset_history_state()
                self._clear_tab_state()
                return
            # else fall through to super() for normal single-char movement

        # ── Tab completion ──────────────────────────────────────────────
        if key == Qt.Key.Key_Tab:
            self._do_tab()
            return

        # Space while a tab completion suffix is selected → accept it,
        # collapse the selection to end, then fall through to insert the space
        if key == Qt.Key.Key_Space and self._tab_matches and self.hasSelectedText():
            end = self.selectionStart() + len(self.selectedText())
            self.setCursorPosition(end)
            self._clear_tab_state()
            # fall through to super() so the space is actually inserted

        # Any printable key resets both history navigation and tab cycling
        if key not in (Qt.Key.Key_Shift, Qt.Key.Key_Control,
                       Qt.Key.Key_Alt,   Qt.Key.Key_Meta,
                       Qt.Key.Key_Home,  Qt.Key.Key_End,
                       Qt.Key.Key_Space):
            self._reset_history_state()
            self._clear_tab_state()
        elif key == Qt.Key.Key_Space and not self._tab_matches:
            self._reset_history_state()
            self._clear_tab_state()

        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # History navigation
    # ------------------------------------------------------------------

    def _history_step(self, direction: int):
        """
        direction: -1 = older (Up), +1 = newer (Down)

        On the very first Up press, lock the current text as the search
        prefix (unless it's fully selected, which means a history entry
        is already showing — in that case keep the existing prefix).
        """
        if not self._history:
            return

        # Lock prefix on first navigation press
        if not self._prefix_search:
            sel = self.selectedText()
            cur = self.text()
            # If nothing is selected (user typed something), use it as prefix
            # If everything is selected (recalled history), keep existing prefix
            if sel != cur:
                self._hist_prefix = cur
            self._prefix_search = True
            # Reset index so we search from the newest entry
            self._hist_idx = -1

        prefix = self._hist_prefix

        if direction == -1:
            # Search backwards (older) from current position
            start = self._hist_idx + 1
            for i in range(start, len(self._history)):
                entry = self._history[-(i + 1)]
                if entry.startswith(prefix):
                    self._hist_idx = i
                    self._set_with_selection(entry)
                    return
        else:
            # Search forwards (newer) from current position
            start = self._hist_idx - 1
            for i in range(start, -1, -1):
                entry = self._history[-(i + 1)]
                if entry.startswith(prefix):
                    self._hist_idx = i
                    self._set_with_selection(entry)
                    return
            # Reached the newest end — restore the original prefix text
            self._hist_idx = -1
            self.setText(self._hist_prefix)
            self.setCursorPosition(len(self._hist_prefix))

    def _reset_history_state(self):
        self._hist_idx      = -1
        self._prefix_search = False
        self._hist_prefix   = ""

    # ------------------------------------------------------------------
    # Tab completion
    # ------------------------------------------------------------------

    def _do_tab(self):
        # _tab_anchor: character position where the prefix STARTS in the line.
        # We store this once and reuse it every cycle so repeated Tab presses
        # always replace exactly the right slice of text.

        if not self._tab_matches:
            # ── Start new cycle ─────────────────────────────────────────
            text   = self.text()
            cursor = self.cursorPosition()

            # If a previous Tab left a selection, work from the selection
            # start so we don't include the highlighted suffix in the prefix.
            if self.hasSelectedText():
                sel = self.selectionStart()
                if sel < cursor:
                    cursor = sel

            # Word immediately left of cursor.
            # Strip trailing punctuation so e.g. "nementa's" → "nementa".
            # Only letters and dashes are valid completion characters.
            before_cursor = text[:cursor]
            prefix = _re.split(r"\s+", before_cursor)[-1]
            prefix = _re.sub(r"[^a-zA-Z-]+$", "", prefix)
            if len(prefix) < 2:
                return

            self._tab_prefix  = prefix
            self._tab_anchor  = cursor - len(prefix)   # fixed anchor position
            self._tab_matches = self._completer.complete(prefix)
            if not self._tab_matches:
                return
            self._tab_idx = 0
        else:
            # ── Advance cycle ────────────────────────────────────────────
            self._tab_idx = (self._tab_idx + 1) % len(self._tab_matches)

        match = self._tab_matches[self._tab_idx]

        # Rebuild the line: everything before anchor + match + everything after
        # the previous match (or prefix on first press).
        full = self.text()
        # The "after" part starts at anchor + len(previous match or prefix).
        # On cycle, the selection covers the previous suffix so we can read
        # "after" as everything past the current selection end.

        # FIX 2: malformed ternary cleaned up — was a single mangled line
        if self.hasSelectedText():
            after_pos = (
                self.selectionStart() + len(self.selectedText())
                if self.selectionStart() < self.selectionEnd()
                else self.selectionEnd()
            )
        else:
            after_pos = self._tab_anchor + len(self._tab_prefix)

        before = full[:self._tab_anchor]
        after  = full[after_pos:]

        new_text = before + match + after
        self.setText(new_text)

        # Select the completed suffix (everything added beyond the typed prefix)
        prefix_end = self._tab_anchor + len(self._tab_prefix)
        match_end  = self._tab_anchor + len(match)
        if match_end > prefix_end:
            self.setSelection(prefix_end, match_end - prefix_end)
        else:
            self.setCursorPosition(match_end)

    def _clear_tab_state(self):
        self._tab_matches = []
        self._tab_idx     = -1
        self._tab_prefix  = ""
        self._tab_anchor  = 0

    def _set_with_selection(self, text: str):
        self.setText(text)
        self.selectAll()


class _InputBar(QWidget):
    """
    Single-line input bar with history, tab-completion, and a Send button.

    Public API
    ----------
    grab_focus()              — steal keyboard focus to the line edit
    feed_completion(text)     — pass MUD output text to the tab completer
    """

    def __init__(self, send_callback, parent=None):
        super().__init__(parent)
        self._send_callback = send_callback
        self._completer     = _TabCompleter()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)

        self._edit = _InputLineEdit(self._completer)
        self._edit.setPlaceholderText("Enter command…")
        font = QFont("Monospace")
        font.setPointSize(11)
        self._edit.setFont(font)
        self._edit.setStyleSheet(
            "QLineEdit {"
            "  background: #0d0d18;"
            "  color: #ddd;"
            "  border: 1px solid #444;"
            "  border-radius: 3px;"
            "  padding: 3px 6px;"
            "}"
            "QLineEdit { selection-background-color: #336; }"
        )
        self._edit.returnPressed.connect(self._on_send)

        send_btn = QPushButton("Send")
        send_btn.setFixedWidth(60)
        send_btn.setStyleSheet(
            "QPushButton {"
            "  background: #204050;"
            "  color: #adf;"
            "  border: 1px solid #446;"
            "  border-radius: 3px;"
            "}"
            "QPushButton:hover { background: #305060; }"
        )
        send_btn.clicked.connect(self._on_send)

        layout.addWidget(self._edit)
        layout.addWidget(send_btn)

    def _on_send(self):
        text = self._edit.text().strip()
        if not text:
            return
        self._edit.add_history(text)
        # Leave the text in the box, fully selected — press Enter again
        # to resend, or just start typing to replace it
        self._edit.selectAll()
        self._send_callback(text)

    def feed_completion(self, text: str):
        """Feed plain MUD text into the tab-completer word list."""
        self._completer.feed(text)

    def setFocus(self, reason=Qt.FocusReason.OtherFocusReason):
        self._edit.setFocus(reason)

    def grab_focus(self):
        self._edit.setFocus()


class _WheelRedirectFilter(QObject):
    """
    Application-level event filter that routes all mouse wheel events to
    the scrollback pane (when open) regardless of which widget the mouse is
    over.  We forward the raw QWheelEvent unchanged so Qt applies the OS
    scroll direction (including natural/reverse scroll) exactly as it would
    for any other widget — no manual direction math here.
    """

    def __init__(self, output_widget, parent=None):
        super().__init__(parent)
        self._output = output_widget

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.Wheel:
            return False

        out = self._output
        split_active = out._split_active
        scrollback   = out._scrollback

        # If the event is already targeted at the scrollback, let Qt handle
        # it normally — no interception needed.
        if obj is scrollback or obj is scrollback.viewport():
            return False

        sb = scrollback.verticalScrollBar()

        if split_active:
            # Use the raw pixel or angle delta Qt already computed for us.
            # pixelDelta is set by trackpads; angleDelta by mice (120 units
            # per notch).  Both already incorporate the OS natural-scroll
            # direction — we just apply them directly to the scrollbar.
            px = event.pixelDelta().y()
            if px != 0:
                sb.setValue(sb.value() - px)
            else:
                # angleDelta: 120 units = 1 notch.  Map to ~3 lines.
                angle = event.angleDelta().y()
                steps = angle / 120.0 * sb.singleStep() * 3
                sb.setValue(sb.value() - int(steps))

            # Close split when scrolled back to the very bottom
            if sb.value() >= sb.maximum() - 5:
                if (event.pixelDelta().y() < 0 or
                        (event.pixelDelta().y() == 0 and event.angleDelta().y() < 0)):
                    out.close_split()
            return True

        else:
            # Split is closed — any upward scroll gesture opens it
            opens = (event.pixelDelta().y() > 0 or
                     (event.pixelDelta().y() == 0 and event.angleDelta().y() > 0))
            if opens:
                out.open_split()
            return True


class _InputFocusFilter(QObject):
    """
    Application-level event filter that redirects printable keypresses to
    the input bar no matter which widget currently has focus.

    Exceptions (focus stays where it is):
      - QLineEdit / QTextEdit widgets that are editable (user is typing there)
      - Any open QDialog or QMenu
      - Modifier-only keypresses (Ctrl, Alt, Shift alone)
    """

    def __init__(self, input_bar, parent=None):
        super().__init__(parent)
        self._input_bar = input_bar

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        from PyQt6.QtWidgets import QLineEdit, QTextEdit, QDialog
        if event.type() != QEvent.Type.KeyPress:
            return False

        key = event.key()

        # If a dialog is open don't interfere at all
        active = QApplication.activeWindow()
        if isinstance(active, QDialog):
            return False

        # Tab is special: Qt's default QLineEdit behaviour is to cycle
        # focus to the next widget, bypassing keyPressEvent entirely.
        # We must intercept it here and call _do_tab() directly, then
        # consume the event so Qt never sees it.
        if key == Qt.Key.Key_Tab:
            edit = self._input_bar._edit
            edit.setFocus()
            edit._do_tab()
            return True   # consumed — Qt must not process this Tab

        # Let pure modifier and function keys through unchanged
        if key in (
            Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt,
            Qt.Key.Key_Meta, Qt.Key.Key_Backtab,
            Qt.Key.Key_Escape, Qt.Key.Key_CapsLock,
            Qt.Key.Key_F1,  Qt.Key.Key_F2,  Qt.Key.Key_F3,
            Qt.Key.Key_F4,  Qt.Key.Key_F5,  Qt.Key.Key_F6,
            Qt.Key.Key_F7,  Qt.Key.Key_F8,  Qt.Key.Key_F9,
            Qt.Key.Key_F10, Qt.Key.Key_F11, Qt.Key.Key_F12,
        ):
            return False

        # If an editable text widget already has focus, let Qt handle it
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit) and not focused.isReadOnly():
            return False
        if isinstance(focused, QTextEdit) and not focused.isReadOnly():
            return False

        # Otherwise steal focus — Qt re-delivers the event to the edit
        self._input_bar.grab_focus()
        return False


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("TinTin++ GUI")
        self.resize(1200, 750)
        self._apply_dark_palette()

        self._tt             = TinTinProcess(self)
        self._active_session = None
        self._graph          = MapGraph()
        self._map_refresh_pending = False
        self._config_dlg: ConfigDialog | None = None   # single non-modal instance

        # ---- central widget -----------------------------------------
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Horizontal splitter: output | map
        self._h_split = QSplitter(Qt.Orientation.Horizontal)
        self._h_split.setHandleWidth(5)
        self._h_split.setStyleSheet(
            "QSplitter::handle { background: #2a2a3a; }"
        )

        self._output = OutputWidget()

        # Enforce 80-char minimum width on the output pane.
        _out_font = QFont("Monospace")
        _out_font.setPointSize(11)
        _char_w   = QFontMetrics(_out_font).horizontalAdvance('M')
        _min_out  = _char_w * 80 + 16
        self._output.setMinimumWidth(_min_out)

        self._right = RightPanel()
        self._right.setMinimumWidth(220)
        self._map    = self._right.map_widget   # keep _map alias for compat

        self._h_split.addWidget(self._output)
        self._h_split.addWidget(self._right)
        self._h_split.setSizes([820, 380])
        self._h_split.setCollapsible(0, False)
        self._h_split.setCollapsible(1, False)

        self._input   = _InputBar(self._send_command)
        self._buttons = ButtonBar()

        main_layout.addWidget(self._h_split, stretch=1)
        main_layout.addWidget(self._input)
        main_layout.addWidget(self._buttons)

        # ---- status bar ---------------------------------------------
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._conn_label = QLabel("● Disconnected")
        self._conn_label.setStyleSheet("color: #884444; padding: 0 8px;")
        self._room_label = QLabel("")
        self._room_label.setStyleSheet("color: #aaa; padding: 0 8px;")
        self._status.addPermanentWidget(self._conn_label)
        self._status.addPermanentWidget(self._room_label)

        # ---- menu ---------------------------------------------------
        self._build_menu()

        # ---- wire signals -------------------------------------------
        self._tt.output_received.connect(self._on_tt_output)
        self._tt.process_died.connect(self._on_tt_died)
        self._buttons.command_requested.connect(self._send_command)
        self._buttons.buttons_changed.connect(self._on_buttons_changed)

        # Debounce map redraws (don't re-render on every single GMCP packet)
        self._map_timer = QTimer(self)
        self._map_timer.setSingleShot(True)
        self._map_timer.setInterval(150)
        self._map_timer.timeout.connect(self._refresh_map)

        # ---- input focus filter — redirect all keypresses to input bar ----
        self._focus_filter = _InputFocusFilter(self._input, self)
        QApplication.instance().installEventFilter(self._focus_filter)

        # ---- wheel filter — route all wheel events to the output widget ----
        self._wheel_filter = _WheelRedirectFilter(self._output, self)
        QApplication.instance().installEventFilter(self._wheel_filter)

        # ---- show session manager on startup; tt++ starts after selection ----
        QTimer.singleShot(100, self._show_session_manager)

    # ------------------------------------------------------------------
    # TinTin++ lifecycle
    # ------------------------------------------------------------------

    def _launch_tt(self, gui_config_path: str = None):
        try:
            self._tt.start()
            self._conn_label.setText("● TinTin++ running")
            self._conn_label.setStyleSheet("color: #44aa44; padding: 0 8px;")
            self._output_local("\x1b[32m[TinTin++ started — type #session <name> <host> <port> to connect]\x1b[0m\n")
        except FileNotFoundError as e:
            self._output_local(f"\x1b[31m[ERROR] {e}\x1b[0m\n")
        except Exception as e:
            self._output_local(f"\x1b[31m[LAUNCH ERROR] {e}\x1b[0m\n")

    def _show_session_manager(self):
        dlg = SessionManager(self)
        dlg.connect_requested.connect(self._connect_session)
        if not dlg.exec():
            # User clicked "Launch without connecting" — start bare tt++
            self._launch_tt()
        self._input.grab_focus()

    def _connect_session(self, session: Session):
        """Called when user picks a session and clicks Connect."""
        self.setWindowTitle(f"TinTin++ GUI — {session.name}")
        self._conn_label.setText(f"● Connecting to {session.name}…")
        self._conn_label.setStyleSheet("color: #aaaa44; padding: 0 8px;")

        # Track active session and restore its saved panel layout + buttons
        self._active_session = session
        if session.panel_layout:
            self._right.restore_layout(session.panel_layout)
        # Load per-session buttons — block signal so connect doesn't trigger save
        self._buttons.blockSignals(True)
        self._buttons.set_buttons(getattr(session, 'buttons', []))
        self._buttons.blockSignals(False)

        # Restore per-session font size (0 means use the application default)
        saved_font = getattr(session, 'font_size', 0)
        if saved_font > 0:
            self._output.font_size = saved_font

        cfg_path = config_file_path(session.name)
        self._launch_tt()

        # Issue #session once tt++ has initialised
        delay = 300
        cmd = f"#session {{{session.name}}} {{{session.host}}} {{{session.port}}}"
        QTimer.singleShot(delay, lambda: self._send_command(cmd))

        # Load GUI config after #session has had time to connect
        if cfg_path.exists():
            p = str(cfg_path)
            QTimer.singleShot(delay + 1500, lambda: self._tt.send(f"#read {{{p}}}"))
            self._output_local(
                f"\x1b[32m[Loading config: {cfg_path}]\x1b[0m\n"
            )

        self._conn_label.setText(f"● {session.name}  {session.host}:{session.port}")
        self._conn_label.setStyleSheet("color: #44aa44; padding: 0 8px;")

        self._input.grab_focus()

    def _on_tt_died(self, code: int):
        self._output_local(f"\n[TinTin++ exited — restarting…]\n")
        self._conn_label.setText("● Restarting…")
        self._conn_label.setStyleSheet("color: #aaaa44; padding: 0 8px;")
        self._save_panel_layout()
        QTimer.singleShot(500, self._restart_and_reconnect)

    def _save_panel_layout(self):
        """Persist right-panel layout, buttons, font size, and all config."""
        if self._active_session is None:
            return

        # Collect current state into the in-memory session object.
        # self._active_session is the single source of truth; callers
        # are responsible for updating it before calling here.
        # We also pick up anything the open dialog may have that hasn't
        # been emitted via saved() yet.
        if self._config_dlg is not None and not self._config_dlg.isHidden():
            for tab in (self._config_dlg._aliases_tab,
                        self._config_dlg._actions_tab,
                        self._config_dlg._timers_tab,
                        self._config_dlg._highlights_tab,
                        self._config_dlg._buttons_tab):
                tab.commit()
            cfg = self._config_dlg.get_config()
            self._active_session.buttons    = cfg.get("buttons",    self._active_session.buttons)
            self._active_session.aliases    = cfg.get("aliases",    self._active_session.aliases)
            self._active_session.actions    = cfg.get("actions",    self._active_session.actions)
            self._active_session.timers     = cfg.get("timers",     self._active_session.timers)
            self._active_session.highlights = cfg.get("highlights", self._active_session.highlights)
            self._active_session.variables  = cfg.get("variables",  self._active_session.variables)

        self._active_session.panel_layout = self._right.get_layout()
        self._active_session.font_size    = getattr(self._output, 'font_size', 0)
        # Always sync buttons from the live bar (covers edits made outside the dialog)
        self._active_session.buttons      = self._buttons.get_buttons()

        # Write to disk: load existing sessions, replace matching entry, save back.
        import sys, traceback
        print(
            f"[_save_panel_layout] saving '{self._active_session.name}': "
            f"buttons={len(self._active_session.buttons)}, "
            f"font_size={self._active_session.font_size}",
            file=sys.stderr,
        )
        try:
            sessions = _load_sessions()
            print(f"[_save_panel_layout] _load_sessions returned {len(sessions)} sessions", file=sys.stderr)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            sessions = []

        matched = False
        for i, s in enumerate(sessions):
            if s.name == self._active_session.name:
                sessions[i] = self._active_session
                matched = True
                break
        if not matched:
            print(f"[_save_panel_layout] session not found in list, appending", file=sys.stderr)
            sessions.append(self._active_session)

        try:
            _save_sessions(sessions)
            print(f"[_save_panel_layout] _save_sessions completed OK", file=sys.stderr)
        except Exception:
            traceback.print_exc(file=sys.stderr)

    def _restart_and_reconnect(self):
        # tt++ is dead — just show the session manager; it will launch
        # tt++ itself when the user picks a session
        QTimer.singleShot(100, self._show_session_manager)

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _send_command(self, text: str):
        self._tt.send(text)
        # Echo locally so you see what you typed
        self._output_local(f"\n\x1b[90m> {text}\x1b[0m\n")

    # FIX 1: removed `import re as _re` that was here inside the method body.
    # The module-level import at the top of the file is used directly.
    def _on_tt_output(self, data: bytes):
        """Receive raw bytes from TinTin++ — parse, render, check for GMCP."""
        # Pass to output widget (handles ANSI parsing internally)
        self._output.feed_raw(data)

        # Feed plain text to tab completer (strip ANSI before word extraction)
        plain = _re.sub(rb'\x1b\[[^a-zA-Z]*[a-zA-Z]', b'', data)
        plain = _re.sub(rb'\x1b.', b'', plain)
        self._input.feed_completion(plain.decode('utf-8', errors='replace'))

        # Check for GMCP room data embedded in the text stream
        lines = data.decode("utf-8", errors="replace").splitlines()
        for line in lines:
            result = try_parse_gmcp_line(line)
            if result:
                pkg, payload = result
                if pkg == "Room.Info":
                    self._graph.ingest_gmcp_room_info(payload)
                    vnum = payload.get("num", "?")
                    name = payload.get("name", "")
                    self._room_label.setText(f"[{vnum}] {name}")
                    if not self._map_timer.isActive():
                        self._map_timer.start()

    def _output_local(self, text: str):
        """Inject a local (non-MUD) message into the output widget."""
        self._output.feed_raw(text.encode("utf-8"))

    def _refresh_map(self):
        self._map.refresh(self._graph)

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _build_menu(self):
        bar = self.menuBar()

        # File menu
        file_menu = bar.addMenu("&File")

        load_map_act = QAction("Load &map XML…", self)
        load_map_act.triggered.connect(self._load_map)
        file_menu.addAction(load_map_act)

        file_menu.addSeparator()
        sessions_act = QAction("&Session Manager…", self)
        sessions_act.setShortcut("Ctrl+Shift+N")
        sessions_act.triggered.connect(self._show_session_manager)
        file_menu.addAction(sessions_act)

        file_menu.addSeparator()
        quit_act = QAction("&Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        # View menu
        view_menu = bar.addMenu("&View")

        toggle_split = QAction("Toggle s&crollback split", self)
        toggle_split.setShortcut("Ctrl+Return")
        toggle_split.triggered.connect(self._output.toggle_split)
        view_menu.addAction(toggle_split)

        self._show_map_action = QAction("Show &Map", self)
        self._show_map_action.setShortcut("Ctrl+M")
        self._show_map_action.setCheckable(True)
        self._show_map_action.setChecked(True)
        self._show_map_action.toggled.connect(self._on_show_map_toggled)
        view_menu.addAction(self._show_map_action)

        font_bigger = QAction("Font &larger", self)
        font_bigger.setShortcut("Ctrl+=")
        font_bigger.triggered.connect(lambda: self._change_font(1))
        view_menu.addAction(font_bigger)

        font_smaller = QAction("Font &smaller", self)
        font_smaller.setShortcut("Ctrl+-")
        font_smaller.triggered.connect(lambda: self._change_font(-1))
        view_menu.addAction(font_smaller)

        view_menu.addSeparator()
        config_act = QAction("&Configuration…", self)
        config_act.setShortcut("Ctrl+,")
        config_act.triggered.connect(self._open_config)
        view_menu.addAction(config_act)

        # Help
        help_menu = bar.addMenu("&Help")
        about_act = QAction("&About", self)
        about_act.triggered.connect(lambda: self._output_local(
            "\n[TinTin++ GUI — a graphical shell for tt++]\n"
            "[https://github.com/]\n\n"
        ))
        help_menu.addAction(about_act)

    # ------------------------------------------------------------------
    # Menu handlers
    # ------------------------------------------------------------------

    def _load_map(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load map XML", str(os.path.expanduser("~")),
            "Map XML (*.xml);;All files (*)"
        )
        if path:
            n = self._graph.load_from_xml(path)
            self._output_local(f"\n[Loaded {n} rooms from {path}]\n")
            self._refresh_map()

    def _on_show_map_toggled(self, checked: bool):
        """Called when the 'Show Map' menu item is toggled."""
        self._right.set_map_visible(checked)

    def _on_buttons_changed(self):
        """
        Called immediately whenever the button list changes (edit, delete,
        config dialog accept).  Saves to session JSON if a session is active,
        otherwise to the global fallback file.
        """
        if self._active_session is not None:
            # Keep in-memory session in sync so _save_panel_layout always
            # has current data even if called before the dialog saves.
            self._active_session.buttons = self._buttons.get_buttons()
            self._save_panel_layout()
        else:
            self._buttons.save_global()

    def _open_config(self):
        """
        Open (or raise) the non-modal ConfigDialog.
        Single instance — re-opening brings the existing dialog to front.
        On first open (or re-open after close), immediately reload live
        state from TinTin++ so the editor reflects what's actually running.
        """
        if self._config_dlg is not None and not self._config_dlg.isHidden():
            self._config_dlg.raise_()
            self._config_dlg.activateWindow()
            # Refresh from tt++ each time the dialog is raised
            if self._tt.running:
                self._config_dlg.reload_from_tt()
            return

        s = self._active_session
        config = {
            # Always read from the live ButtonBar — session.buttons may be []
            # meaning "use defaults", while the bar already has the real list.
            "buttons":    self._buttons.get_buttons(),
            "aliases":    getattr(s, 'aliases',    []) if s else [],
            "actions":    getattr(s, 'actions',    []) if s else [],
            "timers":     getattr(s, 'timers',     []) if s else [],
            "highlights": getattr(s, 'highlights', []) if s else [],
            "variables":  getattr(s, 'variables',  []) if s else [],
        }
        self._config_dlg = ConfigDialog(config, self)

        # Provide the loader factory so the dialog can pull live tt++ state
        def _make_loader():
            loader = TinTinConfigLoader(self._tt, self)
            return loader
        self._config_dlg.set_loader_factory(_make_loader)

        self._config_dlg.saved.connect(self._on_config_saved)
        self._config_dlg.show()

        # Auto-load from tt++ immediately if it's running
        if self._tt.running:
            # Small delay so the dialog is fully painted before the
            # status label updates
            QTimer.singleShot(200, self._config_dlg.reload_from_tt)

    def _on_config_saved(self, config: dict):
        """
        Receive saved config from ConfigDialog:
          1. Push buttons into ButtonBar
          2. Write the session config .tin file
          3. Send #read to apply it live in tt++
          4. Persist everything to session JSON
        """
        # Update button bar
        self._buttons.blockSignals(True)
        self._buttons.set_buttons(config.get("buttons", []))
        self._buttons.blockSignals(False)

        # Write config .tin file and reload it in tt++
        if self._active_session is not None:
            cfg_path = config_file_path(self._active_session.name)
            _has_content = any(
                config.get(k) for k in ("aliases", "actions", "timers", "highlights", "variables")
            )
            if _has_content:
                write_config_file(cfg_path, config)
            if self._tt.running:
                self._tt.send(f"#read {{{cfg_path}}}")
                self._output_local(
                    f"\x1b[32m[Config saved → {cfg_path}]\x1b[0m\n"
                )

        # Update active session in memory then persist
        if self._active_session is not None:
            self._active_session.buttons    = config.get("buttons",    [])
            self._active_session.aliases    = config.get("aliases",    [])
            self._active_session.actions    = config.get("actions",    [])
            self._active_session.timers     = config.get("timers",     [])
            self._active_session.highlights = config.get("highlights", [])
            self._active_session.variables  = config.get("variables",  [])
        self._save_panel_layout()

    def _change_font(self, delta: int):
        new_size = max(7, min(self._output.font_size + delta, 24))
        self._output.font_size = new_size
        self._save_panel_layout()

    # ------------------------------------------------------------------

    def closeEvent(self, event):
        # Collect EVERYTHING before touching the dialog or tt++
        if self._active_session is not None:
            # Flush any in-progress edit in all dialog tabs
            if self._config_dlg is not None:
                for tab in (
                    self._config_dlg._buttons_tab,
                    self._config_dlg._aliases_tab,
                    self._config_dlg._actions_tab,
                    self._config_dlg._timers_tab,
                    self._config_dlg._highlights_tab,
                ):
                    tab.commit()
                full_config = self._config_dlg.get_config()
            else:
                full_config = {
                    "buttons":    self._buttons.get_buttons(),
                    "aliases":    getattr(self._active_session, 'aliases',    []),
                    "actions":    getattr(self._active_session, 'actions',    []),
                    "timers":     getattr(self._active_session, 'timers',     []),
                    "highlights": getattr(self._active_session, 'highlights', []),
                    "variables":  getattr(self._active_session, 'variables',  []),
                }

            # Update in-memory session with everything
            self._active_session.buttons      = full_config.get("buttons",    [])
            self._active_session.aliases      = full_config.get("aliases",    [])
            self._active_session.actions      = full_config.get("actions",    [])
            self._active_session.timers       = full_config.get("timers",     [])
            self._active_session.highlights   = full_config.get("highlights", [])
            self._active_session.variables    = full_config.get("variables",  [])
            self._active_session.panel_layout = self._right.get_layout()
            self._active_session.font_size    = getattr(self._output, 'font_size', 0)

            # Only write the .tin config file if there is actual content —
            # never overwrite an existing file with empty lists.
            cfg_path = config_file_path(self._active_session.name)
            _has_content = any(
                full_config.get(k) for k in ("aliases", "actions", "timers", "highlights", "variables")
            )
            if _has_content:
                write_config_file(cfg_path, full_config)

        # Now safe to close dialog and stop tt++
        if self._config_dlg is not None:
            self._config_dlg.close()
        self._tt.stop()

        # Persist session to JSON
        self._save_panel_layout()
        if self._active_session is None:
            self._buttons.save_global()
        super().closeEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        # Any keypress outside the input bar refocuses it
        if event.key() not in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self._input.grab_focus()
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Theming
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_dark_palette():
        from PyQt6.QtGui import QPalette, QColor
        app = QApplication.instance()
        pal = QPalette()
        pal.setColor(QPalette.ColorRole.Window,          QColor(18, 18, 28))
        pal.setColor(QPalette.ColorRole.WindowText,      QColor(210, 210, 210))
        pal.setColor(QPalette.ColorRole.Base,            QColor(10, 10, 18))
        pal.setColor(QPalette.ColorRole.AlternateBase,   QColor(22, 22, 32))
        pal.setColor(QPalette.ColorRole.ToolTipBase,     QColor(10, 10, 18))
        pal.setColor(QPalette.ColorRole.ToolTipText,     QColor(210, 210, 210))
        pal.setColor(QPalette.ColorRole.Text,            QColor(210, 210, 210))
        pal.setColor(QPalette.ColorRole.Button,          QColor(30, 30, 48))
        pal.setColor(QPalette.ColorRole.ButtonText,      QColor(210, 210, 210))
        pal.setColor(QPalette.ColorRole.BrightText,      QColor(255, 100, 100))
        pal.setColor(QPalette.ColorRole.Link,            QColor(80, 160, 220))
        pal.setColor(QPalette.ColorRole.Highlight,       QColor(50, 90, 160))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
        app.setPalette(pal)
