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

    FIX 6 (scrollback lazy rendering):
    While the scrollback pane is hidden, incoming spans are buffered in
    _pending_spans (capped at _SCROLLBACK_MAX lines worth).  When
    open_split() is called, the buffer is flushed to the scrollback pane
    before it becomes visible.  This eliminates the double-render cost
    during normal play when only the live pane is showing.
    """

    # Rough line estimate per span batch for the pending buffer cap.
    # We cap the pending buffer at SCROLLBACK_MAX lines to bound memory.
    _PENDING_LINE_CAP = _SCROLLBACK_MAX

    def __init__(self, parent=None):
        super().__init__(parent)
        self._parser       = AnsiParser()
        self._split_active = False

        # FIX 6: pending spans accumulated while scrollback is hidden
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

        # FIX 3: pass self so panes hold a direct reference, not a name-walk
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

        # ── Scrollback — FIX 6: lazy ──────────────────────────────────
        if self._split_active:
            # Split is open: write directly, trim, then pin to bottom so
            # new MUD output is always visible (matches live pane behaviour)
            self._scrollback.append_spans(spans)
            self._scrollback.trim_to(_SCROLLBACK_MAX)
        else:
            # Split is hidden: buffer spans, cap by line count
            self._pending_spans.extend(spans)
            for span in spans:
                self._pending_lines += span.text.count('\n')

            # Keep the pending buffer bounded so memory doesn't grow unbounded
            # during a long play session with the split never opened.
            if self._pending_lines > self._PENDING_LINE_CAP:
                self._trim_pending()

    def _trim_pending(self):
        """Drop oldest spans from the pending buffer to stay within cap."""
        excess = self._pending_lines - self._PENDING_LINE_CAP
        while self._pending_spans and excess > 0:
            dropped = self._pending_spans.pop(0)
            excess -= dropped.text.count('\n')
            self._pending_lines -= dropped.text.count('\n')
        self._pending_lines = max(0, self._pending_lines)

    def _flush_pending_to_scrollback(self):
        """Flush buffered spans into the scrollback pane, then clear buffer."""
        if self._pending_spans:
            self._scrollback.append_spans(self._pending_spans)
            self._scrollback.trim_to(_SCROLLBACK_MAX)
            self._pending_spans.clear()
            self._pending_lines = 0

    def clear(self):
        for pane in (self._live, self._scrollback):
            pane.clear()
            pane._line_count = 0
        self._pending_spans.clear()
        self._pending_lines = 0

    def open_split(self):
        if self._split_active:
            return
        self._split_active = True

        # FIX 6: flush buffered history before showing scrollback
        self._flush_pending_to_scrollback()

        self._scrollback.setVisible(True)
        # Position scrollback near the bottom so context is visible
        self._scrollback.pin_to_bottom()
        # Defer re-pin of live pane — Qt resizes it when the splitter shows
        # the scrollback pane, which shifts the viewport before our cursor
        # anchor takes effect.  One event-loop cycle is enough for the
        # layout to settle, then we re-pin cleanly.
        QTimer.singleShot(0, self._live.pin_to_bottom)

    def close_split(self):
        if not self._split_active:
            return
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
