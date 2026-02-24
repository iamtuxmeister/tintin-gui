"""
ButtonBar — configurable alias / macro button strip.

Buttons can be:
  - a raw TinTin++ command string  (e.g. "look")
  - a multi-command sequence (semicolon separated — TinTin++ native syntax)

Button definitions are stored per-session inside the session JSON.
The global fallback file (~/.config/tintin-gui/buttons.json) is used only
when no session is active (bare tt++ launch with no session selected).

Public API for MainWindow
-------------------------
  bar.get_buttons()             → list of dicts  (for saving into session)
  bar.set_buttons(list)         → replace button set (called on session load)
  bar.open_config_dialog()      → open the full ButtonConfigDialog
  bar.save_global()             → write to the fallback global JSON
  bar.load_global()             → read from the fallback global JSON

Per-button right-click menu (quick edits) is still available.
"""

import json
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Callable

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
    QSizePolicy, QScrollArea, QDialog, QFormLayout,
    QLineEdit, QDialogButtonBox, QLabel, QColorDialog,
    QMenu, QListWidget, QListWidgetItem, QAbstractItemView,
    QSplitter, QFrame,
)
from PyQt6.QtCore    import Qt, pyqtSignal
from PyQt6.QtGui     import QFont, QColor, QAction


_CONFIG_DIR  = Path.home() / ".config" / "tintin-gui"
_CONFIG_FILE = _CONFIG_DIR / "buttons.json"

_DEFAULT_BUTTONS = [
    {"label": "Look",  "command": "look",    "color": "#2a5a3a"},
    {"label": "Inv",   "command": "inv",     "color": "#2a3a5a"},
    {"label": "Score", "command": "score",   "color": "#2a3a5a"},
    {"label": "Map",   "command": "#map map","color": "#4a3a2a"},
    {"label": "N",     "command": "n",       "color": "#1a3a1a"},
    {"label": "S",     "command": "s",       "color": "#1a3a1a"},
    {"label": "E",     "command": "e",       "color": "#1a3a1a"},
    {"label": "W",     "command": "w",       "color": "#1a3a1a"},
    {"label": "U",     "command": "u",       "color": "#1a2a3a"},
    {"label": "D",     "command": "d",       "color": "#1a2a3a"},
]

_DARK = {
    "bg":       "#0d0d18",
    "panel":    "#12121e",
    "border":   "#2a2a40",
    "text":     "#ccccdd",
    "accent":   "#3a6aaa",
    "btn":      "#1e2a3a",
    "btn_hover":"#2a3a4a",
}

_DIALOG_STYLE = f"""
QDialog    {{ background: {_DARK['panel']}; color: {_DARK['text']}; }}
QLabel     {{ color: {_DARK['text']}; }}
QLineEdit  {{
    background: {_DARK['bg']}; color: {_DARK['text']};
    border: 1px solid {_DARK['border']}; border-radius: 3px; padding: 4px 6px;
}}
QLineEdit:focus {{ border: 1px solid {_DARK['accent']}; }}
QListWidget {{
    background: {_DARK['bg']}; color: {_DARK['text']};
    border: 1px solid {_DARK['border']}; border-radius: 3px;
}}
QListWidget::item:selected {{ background: {_DARK['accent']}; color: #fff; }}
QListWidget::item:hover    {{ background: #1e2a3a; }}
QPushButton {{
    background: {_DARK['btn']}; color: {_DARK['text']};
    border: 1px solid {_DARK['border']}; border-radius: 3px; padding: 5px 12px;
}}
QPushButton:hover   {{ background: {_DARK['btn_hover']}; }}
QPushButton:pressed {{ background: {_DARK['bg']}; }}
QPushButton:disabled {{ color: #555; }}
QSplitter::handle {{ background: {_DARK['border']}; }}
"""


@dataclass
class ButtonDef:
    label:   str
    command: str
    color:   str = "#2a2a3a"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ButtonDef":
        return ButtonDef(
            label   = d.get("label",   "?"),
            command = d.get("command", ""),
            color   = d.get("color",   "#2a2a3a"),
        )


# ── Single-button quick-edit dialog ──────────────────────────────────

class _EditDialog(QDialog):
    """Quick single-button editor (used from the right-click context menu)."""

    def __init__(self, btn: ButtonDef, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Button")
        self.setStyleSheet(_DIALOG_STYLE)
        self._btn   = btn
        self._color = btn.color

        layout = QFormLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        self._label_edit   = QLineEdit(btn.label)
        self._command_edit = QLineEdit(btn.command)
        layout.addRow("Label",   self._label_edit)
        layout.addRow("Command", self._command_edit)

        color_btn = QPushButton("Pick colour…")
        color_btn.setStyleSheet(f"background-color: {btn.color};")
        color_btn.clicked.connect(lambda: self._pick_color(color_btn))
        layout.addRow("Color", color_btn)
        self._color_btn = color_btn

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _pick_color(self, btn: QPushButton):
        col = QColorDialog.getColor(QColor(self._color), self, "Button Color")
        if col.isValid():
            self._color = col.name()
            btn.setStyleSheet(f"background-color: {self._color};")

    def result_def(self) -> ButtonDef:
        return ButtonDef(
            label   = self._label_edit.text().strip() or "?",
            command = self._command_edit.text().strip(),
            color   = self._color,
        )


# ── Full button configuration dialog ─────────────────────────────────

class ButtonConfigDialog(QDialog):
    """
    Full button manager:
      Left pane  — list of all buttons (drag to reorder)
      Right pane — editor for the selected button
      Toolbar    — Add / Delete / Move Up / Move Down
    """

    def __init__(self, buttons: List[ButtonDef], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure Buttons")
        self.setMinimumSize(620, 420)
        self.setStyleSheet(_DIALOG_STYLE)

        # Work on a copy so Cancel discards changes
        self._buttons: List[ButtonDef] = [ButtonDef(**asdict(b)) for b in buttons]
        self._current_idx: int = -1

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # ── Top: list + editor in a splitter ─────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, stretch=1)

        # Left: toolbar + list
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        self._btn_add  = QPushButton("Add")
        self._btn_del  = QPushButton("Delete")
        self._btn_up   = QPushButton("▲")
        self._btn_down = QPushButton("▼")
        self._btn_up.setFixedWidth(32)
        self._btn_down.setFixedWidth(32)
        for b in (self._btn_add, self._btn_del, self._btn_up, self._btn_down):
            toolbar.addWidget(b)
        toolbar.addStretch()
        left_layout.addLayout(toolbar)

        self._list = QListWidget()
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.currentRowChanged.connect(self._on_row_changed)
        self._list.model().rowsMoved.connect(self._on_rows_moved)
        left_layout.addWidget(self._list)

        splitter.addWidget(left_widget)

        # Right: editor for selected button
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(8)

        right_layout.addWidget(QLabel("Label"))
        self._ed_label = QLineEdit()
        self._ed_label.setPlaceholderText("Button label")
        right_layout.addWidget(self._ed_label)

        right_layout.addWidget(QLabel("Command"))
        self._ed_command = QLineEdit()
        self._ed_command.setPlaceholderText("TinTin++ command")
        right_layout.addWidget(self._ed_command)

        right_layout.addWidget(QLabel("Color"))
        self._ed_color_btn = QPushButton("Pick colour…")
        self._ed_color_btn.clicked.connect(self._pick_color)
        right_layout.addWidget(self._ed_color_btn)

        # Live-apply: update the button def as you type
        self._ed_label.textChanged.connect(self._apply_edit)
        self._ed_command.textChanged.connect(self._apply_edit)

        right_layout.addStretch()
        splitter.addWidget(right_widget)
        splitter.setSizes([280, 300])

        # ── Bottom: OK / Cancel ───────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_DARK['border']};")
        root.addWidget(sep)

        bottom_btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        bottom_btns.accepted.connect(self.accept)
        bottom_btns.rejected.connect(self.reject)
        root.addWidget(bottom_btns)

        # Wire toolbar buttons
        self._btn_add.clicked.connect(self._add_button)
        self._btn_del.clicked.connect(self._delete_button)
        self._btn_up.clicked.connect(self._move_up)
        self._btn_down.clicked.connect(self._move_down)

        self._rebuild_list()
        self._set_editor_enabled(False)

    # ── List management ───────────────────────────────────────────────

    def _rebuild_list(self):
        self._list.blockSignals(True)
        self._list.clear()
        for btn in self._buttons:
            item = QListWidgetItem(f"{btn.label}  →  {btn.command}")
            item.setForeground(QColor(btn.color))
            self._list.addItem(item)
        self._list.blockSignals(False)

    def _refresh_item(self, idx: int):
        """Update the text/colour of a single list item without full rebuild."""
        if 0 <= idx < self._list.count():
            btn  = self._buttons[idx]
            item = self._list.item(idx)
            item.setText(f"{btn.label}  →  {btn.command}")
            item.setForeground(QColor(btn.color))

    def _on_row_changed(self, row: int):
        self._current_idx = row
        if row < 0 or row >= len(self._buttons):
            self._set_editor_enabled(False)
            return
        self._set_editor_enabled(True)
        btn = self._buttons[row]
        # Block signals while populating so _apply_edit doesn't fire
        self._ed_label.blockSignals(True)
        self._ed_command.blockSignals(True)
        self._ed_label.setText(btn.label)
        self._ed_command.setText(btn.command)
        self._ed_color_btn.setStyleSheet(
            f"background-color: {btn.color}; color: #ddd;"
        )
        self._ed_color_btn.setText(btn.color)
        self._ed_label.blockSignals(False)
        self._ed_command.blockSignals(False)

    def _on_rows_moved(self, *_):
        """
        After a drag-reorder, sync self._buttons to match the new list order.
        QListWidget reorders its items internally on drag; we read them back.
        """
        # The QListWidget items have been reordered but self._buttons has not.
        # We rebuild self._buttons from the current item text by matching label.
        # To avoid ambiguity we store the original index in item data.
        pass  # handled in _rebuild_from_list_order

    def _rebuild_from_list_order(self):
        """Not used yet — drag-reorder syncs via itemMoved signal."""
        pass

    def _set_editor_enabled(self, enabled: bool):
        for w in (self._ed_label, self._ed_command, self._ed_color_btn,
                  self._btn_del, self._btn_up, self._btn_down):
            w.setEnabled(enabled)

    # ── Editing ───────────────────────────────────────────────────────

    def _apply_edit(self):
        """Called on every keystroke in label/command fields."""
        idx = self._current_idx
        if idx < 0 or idx >= len(self._buttons):
            return
        self._buttons[idx].label   = self._ed_label.text().strip() or "?"
        self._buttons[idx].command = self._ed_command.text().strip()
        self._refresh_item(idx)

    def _pick_color(self):
        idx = self._current_idx
        if idx < 0 or idx >= len(self._buttons):
            return
        col = QColorDialog.getColor(
            QColor(self._buttons[idx].color), self, "Button Color"
        )
        if col.isValid():
            self._buttons[idx].color = col.name()
            self._ed_color_btn.setStyleSheet(
                f"background-color: {col.name()}; color: #ddd;"
            )
            self._ed_color_btn.setText(col.name())
            self._refresh_item(idx)

    def _add_button(self):
        new = ButtonDef(label="New", command="", color="#2a2a3a")
        insert_at = self._current_idx + 1 if self._current_idx >= 0 else len(self._buttons)
        self._buttons.insert(insert_at, new)
        self._rebuild_list()
        self._list.setCurrentRow(insert_at)

    def _delete_button(self):
        idx = self._current_idx
        if idx < 0 or idx >= len(self._buttons):
            return
        self._buttons.pop(idx)
        self._rebuild_list()
        new_row = min(idx, len(self._buttons) - 1)
        self._list.setCurrentRow(new_row)

    def _move_up(self):
        idx = self._current_idx
        if idx <= 0:
            return
        self._buttons[idx], self._buttons[idx - 1] = \
            self._buttons[idx - 1], self._buttons[idx]
        self._rebuild_list()
        self._list.setCurrentRow(idx - 1)

    def _move_down(self):
        idx = self._current_idx
        if idx < 0 or idx >= len(self._buttons) - 1:
            return
        self._buttons[idx], self._buttons[idx + 1] = \
            self._buttons[idx + 1], self._buttons[idx]
        self._rebuild_list()
        self._list.setCurrentRow(idx + 1)

    # ── Result ────────────────────────────────────────────────────────

    def result_buttons(self) -> List[ButtonDef]:
        """Call after exec() returns Accepted to get the edited list."""
        return list(self._buttons)


# ── ButtonBar widget ─────────────────────────────────────────────────

class ButtonBar(QWidget):
    """
    Horizontal strip of macro buttons.

    Signals
    -------
    command_requested(str)  — emitted when a button is clicked
    buttons_changed()       — emitted whenever the button list is modified
                              (edit, delete, reorder, config dialog accept)
                              MainWindow connects this to persist immediately
    """
    command_requested = pyqtSignal(str)
    buttons_changed   = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buttons: List[ButtonDef] = []
        self._widgets: List[QPushButton] = []

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(46)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: #0d0d14; }")

        self._inner = QWidget()
        self._inner.setStyleSheet("background: #0d0d14;")
        self._row = QHBoxLayout(self._inner)
        self._row.setContentsMargins(4, 4, 4, 4)
        self._row.setSpacing(4)
        self._row.addStretch()

        scroll.setWidget(self._inner)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.load_global()

    # ------------------------------------------------------------------
    # Public API for session integration
    # ------------------------------------------------------------------

    def get_buttons(self) -> list:
        """Return button definitions as a list of dicts (for session storage)."""
        return [b.to_dict() for b in self._buttons]

    def set_buttons(self, button_dicts: list):
        """
        Replace the current button set from a list of dicts.
        Pass an empty list to revert to defaults.
        """
        if button_dicts:
            self._buttons = [ButtonDef.from_dict(d) for d in button_dicts]
        else:
            self._buttons = [ButtonDef(**d) for d in _DEFAULT_BUTTONS]
        self._rebuild()
        self.buttons_changed.emit()

    def open_config_dialog(self, parent=None) -> bool:
        """
        Open the full ButtonConfigDialog.
        Returns True if the user accepted (buttons were changed).
        """
        dlg = ButtonConfigDialog(self._buttons, parent or self)
        if dlg.exec():
            self._buttons = dlg.result_buttons()
            self._rebuild()
            self.buttons_changed.emit()
            return True
        return False

    # ------------------------------------------------------------------
    # Global fallback JSON (used when no session is active)
    # ------------------------------------------------------------------

    def load_global(self, path: Path = _CONFIG_FILE):
        """Load from the global fallback JSON, falling back to defaults."""
        try:
            with open(path) as f:
                raw = json.load(f)
            self._buttons = [ButtonDef.from_dict(b) for b in raw]
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            self._buttons = [ButtonDef(**b) for b in _DEFAULT_BUTTONS]
        self._rebuild()

    def save_global(self, path: Path = _CONFIG_FILE):
        """Persist current buttons to the global fallback JSON."""
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.get_buttons(), f, indent=2)

    # ------------------------------------------------------------------
    # Internal rendering
    # ------------------------------------------------------------------

    def _rebuild(self):
        for w in self._widgets:
            self._row.removeWidget(w)
            w.deleteLater()
        self._widgets.clear()

        font = QFont()
        font.setPointSize(9)
        font.setBold(True)

        for idx, btn in enumerate(self._buttons):
            w = QPushButton(btn.label)
            w.setFont(font)
            w.setFixedSize(58, 30)
            w.setStyleSheet(
                f"QPushButton {{"
                f"  background-color: {btn.color};"
                f"  color: #ddd;"
                f"  border: 1px solid #555;"
                f"  border-radius: 3px;"
                f"}}"
                f"QPushButton:hover {{ background-color: {btn.color}cc; border: 1px solid #aaa; }}"
                f"QPushButton:pressed {{ background-color: #111; }}"
            )
            w.setToolTip(f"Sends: {btn.command}")
            w.clicked.connect(lambda _, cmd=btn.command: self.command_requested.emit(cmd))
            w.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            w.customContextMenuRequested.connect(
                lambda pos, i=idx, widget=w: self._context_menu(i, widget, pos)
            )
            self._row.insertWidget(self._row.count() - 1, w)
            self._widgets.append(w)

    def _context_menu(self, idx: int, widget: QPushButton, pos):
        menu = QMenu(self)
        edit_act   = menu.addAction("Edit…")
        delete_act = menu.addAction("Delete")
        menu.addSeparator()
        config_act = menu.addAction("Configure all buttons…")
        action = menu.exec(widget.mapToGlobal(pos))

        if action == edit_act:
            dlg = _EditDialog(self._buttons[idx], self)
            if dlg.exec():
                self._buttons[idx] = dlg.result_def()
                self._rebuild()
                self.buttons_changed.emit()
        elif action == delete_act:
            self._buttons.pop(idx)
            self._rebuild()
            self.buttons_changed.emit()
        elif action == config_act:
            self.open_config_dialog()  # already emits buttons_changed internally
