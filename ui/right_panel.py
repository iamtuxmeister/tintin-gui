"""
RightPanel — map (show/hide) + vertical stack of tabbed text boxes.

Layout
------
RightPanel
  └── QSplitter (Vertical)
        ├── MapWidget       ← sits above text boxes, hidden via View menu
        ├── _TextBox        ← QTabWidget of scrollable TextPanes
        └── _TextBox ...    ← additional splits

The map is NOT a tab. It is a plain widget in the splitter.
Show/hide is controlled externally by calling toggle_map() or set_map_visible().

Right-click menu on every _TextBox (tab bar or content area):
  ➕  Add tab
  ⬇  Split below
  ⬆  Split above
  ─────────────
  ✏️  Rename tab
  ✖  Close tab        (if >1 tabs)
  ─────────────
  🗑  Remove this pane (if >1 text boxes)
"""

from __future__ import annotations
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QSplitter, QTabWidget,
    QSizePolicy, QMenu, QInputDialog, QTextEdit, QToolButton,
)
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui  import QColor, QPalette, QTextCursor

from ui.map_widget import MapWidget


# ── Styles ────────────────────────────────────────────────────────────

_BG   = "#0d0d18"
_PANE = "#12121e"
_BDR  = "#2a2a3a"
_ACC  = "#3a6aaa"
_TXT  = "#ccccdd"

_TAB_STYLE = f"""
QTabWidget::pane {{ border: 1px solid {_BDR}; background: {_BG}; }}
QTabBar::tab {{
    background: {_PANE}; color: #aaa;
    border: 1px solid {_BDR}; border-bottom: none;
    padding: 3px 10px; min-width: 50px; font-size: 10pt;
}}
QTabBar::tab:selected {{
    background: #1e2a3a; color: #ddd;
    border-bottom: 1px solid #1e2a3a;
}}
QTabBar::tab:hover    {{ background: #1a1a2e; color: #ccc; }}
QTabBar::tab:!selected {{ margin-top: 2px; }}
"""

_CORNER_BTN_STYLE = f"""
QToolButton {{
    background: {_PANE}; color: #888;
    border: 1px solid {_BDR}; border-bottom: none;
    padding: 2px 7px; font-size: 14px; font-weight: bold;
}}
QToolButton:hover   {{ background: #1e2a3a; color: #ddd; }}
QToolButton:pressed {{ background: {_BG}; }}
"""

_MENU_STYLE = f"""
QMenu {{
    background: #1a1a2e; color: {_TXT};
    border: 1px solid {_BDR}; padding: 2px;
}}
QMenu::item {{ padding: 4px 24px 4px 8px; }}
QMenu::item:selected {{ background: {_ACC}; }}
QMenu::separator {{ height: 1px; background: {_BDR}; margin: 3px 4px; }}
"""

_SPLITTER_CSS = "QSplitter::handle { background: #2a2a3a; min-height: 6px; }"


# ── TextPane ──────────────────────────────────────────────────────────

class TextPane(QTextEdit):
    """Scrollable read-only text pane. Right-clicks bubble to _TextBox."""

    def __init__(self, title: str = "Text", parent=None):
        super().__init__(parent)
        self._title = title
        self.setReadOnly(True)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(
            f"QTextEdit {{ background:{_BG}; color:{_TXT}; border:none;"
            f"font-family:Monospace; font-size:10pt; }}"
        )
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor(_BG))
        self.setPalette(pal)
        self._cur = QTextCursor(self.document())
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._bubble)

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, v: str):
        self._title = v

    def feed(self, text: str):
        self._cur.movePosition(QTextCursor.MoveOperation.End)
        self._cur.insertText(text)
        self.setTextCursor(self._cur)
        self.ensureCursorVisible()

    def feed_html(self, html: str):
        self._cur.movePosition(QTextCursor.MoveOperation.End)
        self._cur.insertHtml(html)
        self.setTextCursor(self._cur)
        self.ensureCursorVisible()

    def clear_content(self):
        self.clear()
        self._cur = QTextCursor(self.document())

    def _bubble(self, pos: QPoint):
        p = self.parent()
        while p:
            if isinstance(p, _TextBox):
                p._show_menu(self.mapToGlobal(pos), p.indexOf(self))
                return
            p = p.parent()


# ── _TextBox ──────────────────────────────────────────────────────────

class _TextBox(QTabWidget):
    """One tabbed section in the splitter. All tabs are TextPanes."""

    def __init__(self, panel: "RightPanel", parent=None):
        super().__init__(parent)
        self._panel = panel
        self.setStyleSheet(_TAB_STYLE)
        self.setTabPosition(QTabWidget.TabPosition.North)
        self.setMovable(True)
        self.setDocumentMode(True)

        btn = QToolButton()
        btn.setText(" + ")
        btn.setToolTip("Add tab")
        btn.setStyleSheet(_CORNER_BTN_STYLE)
        btn.clicked.connect(lambda: self._add_tab_dialog())
        self.setCornerWidget(btn, Qt.Corner.TopRightCorner)

        self.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabBar().customContextMenuRequested.connect(self._on_tabbar_rclick)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_body_rclick)

    # ── Context menu ──────────────────────────────────────────────────

    def _on_tabbar_rclick(self, pos: QPoint):
        self._show_menu(self.tabBar().mapToGlobal(pos), self.tabBar().tabAt(pos))

    def _on_body_rclick(self, pos: QPoint):
        self._show_menu(self.mapToGlobal(pos), self.currentIndex())

    def _show_menu(self, global_pos: QPoint, tab_idx: int = -1):
        menu = QMenu(self)
        menu.setStyleSheet(_MENU_STYLE)

        menu.addAction("➕  Add tab",     lambda: self._add_tab_dialog())
        menu.addSeparator()
        menu.addAction("⬇  Split below", lambda: self._panel._split_below(self))
        menu.addAction("⬆  Split above", lambda: self._panel._split_above(self))

        if tab_idx >= 0:
            menu.addSeparator()
            menu.addAction("✏️  Rename tab",  lambda: self._rename_tab(tab_idx))
            if self.count() > 1:
                menu.addAction("✖  Close tab", lambda: self._close_tab(tab_idx))

        if self._panel._text_box_count() > 1:
            menu.addSeparator()
            menu.addAction("🗑  Remove this pane", lambda: self._panel._remove_box(self))

        menu.exec(global_pos)

    # ── Tab operations ────────────────────────────────────────────────

    def _add_tab_dialog(self):
        title, ok = QInputDialog.getText(self, "New Tab", "Tab name:", text="Text")
        if ok and title.strip():
            self.add_text_tab(title.strip())

    def add_text_tab(self, title: str) -> TextPane:
        pane = TextPane(title)
        idx  = self.addTab(pane, title)
        self.setCurrentIndex(idx)
        return pane

    def _rename_tab(self, idx: int):
        cur = self.tabText(idx)
        title, ok = QInputDialog.getText(self, "Rename Tab", "New name:", text=cur)
        if ok and title.strip():
            self.setTabText(idx, title.strip())
            w = self.widget(idx)
            if isinstance(w, TextPane):
                w.title = title.strip()

    def _close_tab(self, idx: int):
        if self.count() > 1:
            self.removeTab(idx)

    # ── Serialisation ─────────────────────────────────────────────────

    def get_data(self) -> dict:
        return {
            "tabs":       [{"title": self.tabText(i)} for i in range(self.count())],
            "active_tab": self.currentIndex(),
        }


# ── RightPanel ────────────────────────────────────────────────────────

class RightPanel(QWidget):
    """
    MapWidget (show/hide via View menu) + N _TextBox panes in a splitter.

    Layout dict:
        {
          "map_visible":    true,
          "boxes":          [{"tabs": [{"title": "Text"}], "active_tab": 0}],
          "splitter_sizes": [280, 220]
        }
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.setHandleWidth(6)
        self._splitter.setStyleSheet(_SPLITTER_CSS)
        root.addWidget(self._splitter)

        self._map = MapWidget()
        self._map.setMinimumHeight(60)
        self._splitter.addWidget(self._map)
        self._splitter.setCollapsible(0, True)

        self._map_visible      = True
        self._saved_map_height = 0

        # Default: one text box
        box = _TextBox(self)
        self._splitter.addWidget(box)
        box.add_text_tab("Text")

    # ── Public ────────────────────────────────────────────────────────

    @property
    def map_widget(self) -> MapWidget:
        return self._map

    @property
    def map_visible(self) -> bool:
        return self._map_visible

    def set_map_visible(self, visible: bool):
        if visible == self._map_visible:
            return
        self._map_visible = visible
        sizes = self._splitter.sizes()
        total = sum(sizes)
        if not visible:
            self._saved_map_height = sizes[0]
            new = [0] + sizes[1:]
            self._splitter.setSizes(new)
        else:
            restore  = self._saved_map_height or max(total // 3, 80)
            n_text   = len(sizes) - 1
            rest     = total - restore
            per      = (rest // n_text) if n_text else rest
            new      = [restore] + [per] * n_text
            self._splitter.setSizes(new)

    def toggle_map(self):
        self.set_map_visible(not self._map_visible)

    def all_boxes(self) -> list[_TextBox]:
        return [
            self._splitter.widget(i)
            for i in range(self._splitter.count())
            if isinstance(self._splitter.widget(i), _TextBox)
        ]

    def _text_box_count(self) -> int:
        return len(self.all_boxes())

    def get_text_pane(self, title: str) -> Optional[TextPane]:
        for box in self.all_boxes():
            for i in range(box.count()):
                w = box.widget(i)
                if isinstance(w, TextPane) and w.title == title:
                    return w
        return None

    def all_text_panes(self) -> list[TextPane]:
        out = []
        for box in self.all_boxes():
            for i in range(box.count()):
                w = box.widget(i)
                if isinstance(w, TextPane):
                    out.append(w)
        return out

    # ── Layout persistence ────────────────────────────────────────────

    def get_layout(self) -> dict:
        return {
            "map_visible":    self._map_visible,
            "boxes":          [b.get_data() for b in self.all_boxes()],
            "splitter_sizes": self._splitter.sizes(),
        }

    def restore_layout(self, layout: dict):
        if not layout or "boxes" not in layout:
            return
        for box in self.all_boxes():
            box.setParent(None)
        for bdata in layout.get("boxes", []):
            box = _TextBox(self)
            self._splitter.addWidget(box)
            for td in bdata.get("tabs", [{"title": "Text"}]):
                box.add_text_tab(td.get("title", "Text"))
            active = bdata.get("active_tab", 0)
            if 0 <= active < box.count():
                box.setCurrentIndex(active)
        sizes = layout.get("splitter_sizes")
        if sizes and len(sizes) == self._splitter.count():
            self._splitter.setSizes(sizes)
        else:
            self._equalise()
        # Restore map visibility after sizes are set
        saved_vis = layout.get("map_visible", True)
        if not saved_vis:
            self._map_visible = True   # force the toggle to run correctly
            self.set_map_visible(False)

    # ── Splits ────────────────────────────────────────────────────────

    def _split_below(self, box: _TextBox):
        idx = self._box_splitter_idx(box)
        new = _TextBox(self)
        self._splitter.insertWidget(idx + 1, new)
        title, ok = QInputDialog.getText(self, "New Pane", "First tab name:", text="Text")
        new.add_text_tab(title.strip() if ok and title.strip() else "Text")
        self._equalise()

    def _split_above(self, box: _TextBox):
        idx = self._box_splitter_idx(box)
        new = _TextBox(self)
        self._splitter.insertWidget(idx, new)
        title, ok = QInputDialog.getText(self, "New Pane", "First tab name:", text="Text")
        new.add_text_tab(title.strip() if ok and title.strip() else "Text")
        self._equalise()

    def _remove_box(self, box: _TextBox):
        if self._text_box_count() <= 1:
            return
        box.setParent(None)

    def _box_splitter_idx(self, box: _TextBox) -> int:
        for i in range(self._splitter.count()):
            if self._splitter.widget(i) is box:
                return i
        return self._splitter.count() - 1

    def _equalise(self):
        n = self._splitter.count()
        if n == 0:
            return
        h = self._splitter.height() or 600
        self._splitter.setSizes([h // n] * n)
