"""
OutputWidget — ANSI-aware split scrollback display.

Design
------
There is ONE document shared by both panes — the scrollback pane and the
live pane show different views into it via QTextEdit.

Actually, simpler and more reliable: two separate documents, scrollback gets
all history, live pane is capped and ALWAYS pinned to the bottom with NO
scrollbar.  The scrollback pane appears above when the split is active and
is the only scrollable surface.

Live pane rules:
  - Vertical scrollbar: NEVER shown
  - Horizontal scrollbar: shown if line is wider than window
  - Always pinned to bottom — every ingest() call scrolls it down
  - Never receives wheel events for scrolling (wheel opens split instead)

Scrollback pane rules:
  - Only visible when split is active
  - Has full scrollbar, user can browse freely
  - Scrolling to the bottom closes the split

FIX 3: _LivePane and _ScrollbackPane now receive an explicit OutputWidget
reference at construction time instead of walking the parent chain and
comparing type names — more robust and avoids circular import fragility.

FIX 6: Scrollback is lazy — spans are buffered while the scrollback pane is
hidden, then flushed in a single batch when open_split() is called.  This
eliminates the double-render cost during normal play when the split is closed.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QSplitter, QTextEdit
from PyQt6.QtCore    import Qt, QTimer
from PyQt6.QtGui     import (
    QTextCharFormat, QTextCursor, QColor, QFont, QKeyEvent,
    QPalette, QWheelEvent,
)

from core.ansi_parser import AnsiParser, AnsiSpan, TextStyle


_LIVE_MAX_LINES    = 500
_SCROLLBACK_MAX    = 10_000
_BG                = QColor(10, 10, 10)
_FG                = QColor(200, 200, 200)

# Bright variants of the 8 standard ANSI colours (indices 8-15)
_ANSI_BRIGHT = [
    QColor( 85,  85,  85),  # bright black  (dark grey)
    QColor(255,  85,  85),  # bright red
    QColor( 85, 255,  85),  # bright green
    QColor(255, 255,  85),  # bright yellow
    QColor( 85,  85, 255),  # bright blue
    QColor(255,  85, 255),  # bright magenta
    QColor( 85, 255, 255),  # bright cyan
    QColor(255, 255, 255),  # bright white
]


def _make_fmt(style: TextStyle, font: QFont) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setFont(font)
    fg, bg = style.fg, style.bg

    # Bold-as-bright: if bold is set and fg came from a standard 30-37
    # colour code, upgrade it to the bright variant (8-15).
    # This matches classic terminal behaviour where bold+colour = bright colour.
    if style.bold and style._fg_base_idx >= 0:
        fg = _ANSI_BRIGHT[style._fg_base_idx]
    elif fg:
        fg = QColor(*fg)
    else:
        fg = _FG

    if style.reverse:
        fg, bg = (QColor(*bg) if bg else _BG), fg

    fmt.setForeground(fg if isinstance(fg, QColor) else (QColor(*fg) if fg else _FG))
    if bg:
        fmt.setBackground(QColor(*bg) if isinstance(bg, tuple) else bg)

    if style.bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    if style.italic:
        fmt.setFontItalic(True)
    if style.underline:
        fmt.setFontUnderline(True)
    if style.strikethrough:
        fmt.setFontStrikeOut(True)
    return fmt


class _Pane(QTextEdit):
    """Base read-only pane with ANSI append support."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Base, _BG)
        pal.setColor(QPalette.ColorRole.Text, _FG)
        self.setPalette(pal)

        self._font = QFont("Monospace")
        self._font.setStyleHint(QFont.StyleHint.TypeWriter)
        self._font.setPointSize(11)
        self.setFont(self._font)

        self._cur        = QTextCursor(self.document())
        self._line_count = 0

    @property
    def base_font(self):
        return self._font

    def set_font_size(self, pt: int):
        self._font.setPointSize(pt)
        self.setFont(self._font)

    def append_spans(self, spans: list):
        self._cur.movePosition(QTextCursor.MoveOperation.End)
        for span in spans:
            text = span.text.replace('\r\n', '\n').replace('\r', '')
            if not text:
                continue
            self._line_count += text.count('\n')
            self._cur.insertText(text, _make_fmt(span.style, self._font))

    def prepend_spans(self, spans: list):
        """Insert spans at the very top of the document (for backfill)."""
        c = QTextCursor(self.document())
        c.movePosition(QTextCursor.MoveOperation.Start)
        for span in spans:
            text = span.text.replace('\r\n', '\n').replace('\r', '')
            if not text:
                continue
            self._line_count += text.count('\n')
            c.insertText(text, _make_fmt(span.style, self._font))
            # Advance past what we just inserted so the next span follows it
            # (cursor stays at the end of the inserted block, which is correct
            # since we're building top-to-bottom within this call)

    def trim_to(self, max_lines: int):
        excess = self._line_count - max_lines
        if excess <= 0:
            return
        c = QTextCursor(self.document())
        c.movePosition(QTextCursor.MoveOperation.Start)
        c.movePosition(QTextCursor.MoveOperation.Down,
                       QTextCursor.MoveMode.KeepAnchor, excess)
        c.removeSelectedText()
        self._line_count = max_lines

    def pin_to_bottom(self):
        """Always call this after appending to a live (non-scrollable) pane."""
        self._cur.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(self._cur)
        self.ensureCursorVisible()


class _LivePane(_Pane):
    """
    Bottom pane — always pinned to latest output, no scrollbar ever.

    FIX 3: receives an explicit OutputWidget reference instead of walking
    the parent chain and doing a fragile type-name string comparison.
    """

    # FIX 3: accept output_widget reference directly
    def __init__(self, output_widget: "OutputWidget", parent=None):
        super().__init__(parent)
        self._ow = output_widget

    def wheelEvent(self, event: QWheelEvent):
        # Don't scroll the live pane — notify OutputWidget to open the split
        self._ow._on_wheel(event.angleDelta().y())
        # Do NOT call super() — live pane never scrolls via wheel


class _ScrollbackPane(_Pane):
    """
    Top pane — freely scrollable history, visible only when split is active.
    Scrolling to the very bottom closes the split.

    FIX 3: receives an explicit OutputWidget reference (same rationale as
    _LivePane above).
    """

    # FIX 3: accept output_widget reference directly
    def __init__(self, output_widget: "OutputWidget", parent=None):
        super().__init__(parent)
        self._ow = output_widget
        # Scrollback DOES get a vertical scrollbar
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

    def wheelEvent(self, event: QWheelEvent):
        # Wheel events are handled globally by _WheelRedirectFilter in
        # main_window.py which calls OutputWidget._on_wheel. This handler
        # is only reached if the filter is not installed (e.g. in tests).
        self._ow._on_wheel(event.angleDelta().y())


class OutputWidget(QWidget):
    """
    Split-scrollback output widget.

    API:
        feed_raw(bytes)    — parse + render raw bytes from tt++
        ingest(spans)      — render pre-parsed spans
        open_split()       — show scrollback pane above live pane
        close_split()      — hide scrollback, live resumes auto-scroll
        toggle_split()     — flip state
        font_size          — int property

    Scrollback open strategy
    ------------------------
    Two logical regions are maintained while the split is closed:

      _pending_spans  — older history (oldest first, capped at
                        _SCROLLBACK_MAX lines total).
      _tail_spans     — the most recent _TAIL_LINES lines, extracted
                        from _pending_spans at the moment open_split()
                        is called.

    On open_split():
      1. _tail_spans is written synchronously (~100 lines) and the pane
         is pinned to the bottom.  The user sees recent content instantly.
      2. A zero-interval QTimer then *prepends* _pending_spans in chunks
         from the end (newest older → oldest), so the document builds up
         above the tail with the correct chronological order.

    On close_split() during a prepend flush:
      The timer is cancelled, the scrollback document is cleared, and
      _split_tail is restored to _pending_spans so the full buffer is
      consistent for the next open.
    """

    _TAIL_LINES       = 100    # lines written synchronously on open
    _FLUSH_CHUNK      = 300    # spans prepended per event-loop tick
    _PENDING_LINE_CAP = _SCROLLBACK_MAX

    def __init__(self, parent=None):
        super().__init__(parent)
        self._parser       = AnsiParser()
        self._split_active = False
        self._flush_timer  = None    # QTimer used during async prepend
        self._split_tail: list = []  # tail saved during in-progress flush

        self._pending_spans: list[AnsiSpan] = []
        self._pending_lines: int            = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._splitter.setHandleWidth(5)
        self._splitter.setStyleSheet(
            "QSplitter::handle { background: #3c3c50; }"
        )

        self._scrollback = _ScrollbackPane(self)
        self._scrollback.setVisible(False)

        self._live = _LivePane(self)

        self._splitter.addWidget(self._scrollback)
        self._splitter.addWidget(self._live)
        self._splitter.setSizes([300, 200])

        layout.addWidget(self._splitter)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed_raw(self, data: bytes):
        spans = self._parser.feed(data)
        self.ingest(spans)

    def ingest(self, spans: list):
        if not spans:
            return

        # ── Live pane — always updated, capped, pinned to bottom ──────
        self._live.append_spans(spans)
        self._live.trim_to(_LIVE_MAX_LINES)
        self._live.pin_to_bottom()

        # ── Scrollback ────────────────────────────────────────────────
        if self._split_active:
            # New content always goes to the bottom of the scrollback.
            # The prepend timer is adding old history above the tail
            # concurrently, which is fine — live content belongs at the end.
            self._scrollback.append_spans(spans)
            self._scrollback.trim_to(_SCROLLBACK_MAX)
        else:
            self._pending_spans.extend(spans)
            for span in spans:
                self._pending_lines += span.text.count('\n')
            if self._pending_lines > self._PENDING_LINE_CAP:
                self._trim_pending()

    def _trim_pending(self):
        excess = self._pending_lines - self._PENDING_LINE_CAP
        while self._pending_spans and excess > 0:
            dropped = self._pending_spans.pop(0)
            excess -= dropped.text.count('\n')
            self._pending_lines -= dropped.text.count('\n')
        self._pending_lines = max(0, self._pending_lines)

    # ------------------------------------------------------------------
    # Tail helpers
    # ------------------------------------------------------------------

    def _split_off_tail(self) -> tuple[list, list]:
        """
        Partition _pending_spans into (older, tail) where tail holds the
        last _TAIL_LINES lines.  Returns (older_spans, tail_spans).
        """
        lines = 0
        for i in range(len(self._pending_spans) - 1, -1, -1):
            lines += self._pending_spans[i].text.count('\n')
            if lines >= self._TAIL_LINES:
                return self._pending_spans[:i], self._pending_spans[i:]
        # Everything fits inside the tail window
        return [], list(self._pending_spans)

    # ------------------------------------------------------------------
    # Async prepend flush
    # ------------------------------------------------------------------

    def _start_prepend_flush(self):
        if self._flush_timer is not None:
            return
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(False)
        self._flush_timer.setInterval(0)
        self._flush_timer.timeout.connect(self._prepend_chunk)
        self._flush_timer.start()

    def _prepend_chunk(self):
        """
        Take a chunk from the END of _pending_spans and prepend it to the
        scrollback document.  Processing end-first and prepending each time
        ensures chronological order: oldest ends up at the document top.

        After each prepend the scrollbar is shifted by the exact pixel height
        that was added above, so whatever the user is reading stays stationary.
        If they are at the bottom (just opened the split) this naturally keeps
        them at the bottom.  If they have scrolled up it preserves their position.
        """
        if not self._pending_spans:
            self._flush_timer.stop()
            self._flush_timer.deleteLater()
            self._flush_timer = None
            self._split_tail = []   # tail is now part of the document
            self._scrollback.trim_to(_SCROLLBACK_MAX)
            return

        sb      = self._scrollback.verticalScrollBar()
        old_max = sb.maximum()
        old_val = sb.value()

        chunk = self._pending_spans[-self._FLUSH_CHUNK:]
        del self._pending_spans[-self._FLUSH_CHUNK:]
        for span in chunk:
            self._pending_lines -= span.text.count('\n')
        self._pending_lines = max(0, self._pending_lines)

        self._scrollback.prepend_spans(chunk)

        # Compensate for the height added at the top so the viewport is stable
        new_max = sb.maximum()
        sb.setValue(old_val + (new_max - old_max))

    def _stop_flush(self):
        """Cancel an in-progress flush and restore buffer consistency."""
        if self._flush_timer is None:
            return
        self._flush_timer.stop()
        self._flush_timer.deleteLater()
        self._flush_timer = None
        # The scrollback has a partial write (tail + some prepended chunks).
        # Clear it and put the tail back so the next open starts fresh.
        self._scrollback.clear()
        self._scrollback._line_count = 0
        if self._split_tail:
            self._pending_spans.extend(self._split_tail)
            for span in self._split_tail:
                self._pending_lines += span.text.count('\n')
            self._split_tail = []

    # ------------------------------------------------------------------
    # Split open / close / clear
    # ------------------------------------------------------------------

    def clear(self):
        self._stop_flush()
        self._split_tail = []
        for pane in (self._live, self._scrollback):
            pane.clear()
            pane._line_count = 0
        self._pending_spans.clear()
        self._pending_lines = 0

    def open_split(self):
        if self._split_active:
            return
        self._split_active = True
        self._scrollback.setVisible(True)

        if self._pending_spans:
            older, tail = self._split_off_tail()
            self._split_tail      = tail
            self._pending_spans   = older
            self._pending_lines   = sum(s.text.count('\n') for s in older)

            # Write the tail synchronously — instant, ~100 lines
            self._scrollback.append_spans(tail)
            self._scrollback.pin_to_bottom()

            if self._pending_spans:
                self._start_prepend_flush()
        else:
            self._scrollback.pin_to_bottom()

        QTimer.singleShot(0, self._live.pin_to_bottom)

    def close_split(self):
        if not self._split_active:
            return
        self._stop_flush()
        self._split_active = False
        self._scrollback.setVisible(False)
        self._live.pin_to_bottom()

    def toggle_split(self):
        if self._split_active:
            self.close_split()
        else:
            self.open_split()

    @property
    def font_size(self) -> int:
        return self._live.base_font.pointSize()

    @font_size.setter
    def font_size(self, pt: int):
        self._live.set_font_size(pt)
        self._scrollback.set_font_size(pt)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_wheel(self, delta_y: int):
        """Fallback handler for when the wheel filter is not installed."""
        if not self._split_active:
            if delta_y > 0:
                self.open_split()

    def keyPressEvent(self, event: QKeyEvent):
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.toggle_split()
        elif event.key() == Qt.Key.Key_PageUp:
            self.open_split()
            sb = self._scrollback.verticalScrollBar()
            sb.setValue(sb.value() - self._scrollback.height())
        elif event.key() == Qt.Key.Key_PageDown:
            if self._split_active:
                sb = self._scrollback.verticalScrollBar()
                sb.setValue(sb.value() + self._scrollback.height())
                if sb.value() >= sb.maximum() - 5:
                    self.close_split()
        else:
            super().keyPressEvent(event)
