"""
ButtonBar — configurable alias / macro button strip.

Buttons can be:
  - a raw TinTin++ command string (e.g. "#alias look;#alias inv")
  - a multi-command sequence (semicolon separated — TinTin++ native syntax)

Buttons are stored in a simple JSON file (default: ~/.config/tintin-gui/buttons.json)
so they persist across sessions.

The bar can be shown horizontally (bottom of screen) or vertically (side panel).
"""

import json
import os
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Callable

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
    QSizePolicy, QScrollArea, QDialog, QFormLayout,
    QLineEdit, QDialogButtonBox, QLabel, QColorDialog,
    QMenu,
)
from PyQt6.QtCore    import Qt, pyqtSignal
from PyQt6.QtGui     import QFont, QColor, QAction


_CONFIG_DIR  = Path.home() / ".config" / "tintin-gui"
_CONFIG_FILE = _CONFIG_DIR / "buttons.json"

_DEFAULT_BUTTONS = [
    {"label": "Look",     "command": "look",           "color": "#2a5a3a"},
    {"label": "Inv",      "command": "inv",             "color": "#2a3a5a"},
    {"label": "Score",    "command": "score",           "color": "#2a3a5a"},
    {"label": "Map",      "command": "#map map",        "color": "#4a3a2a"},
    {"label": "N",        "command": "n",               "color": "#1a3a1a"},
    {"label": "S",        "command": "s",               "color": "#1a3a1a"},
    {"label": "E",        "command": "e",               "color": "#1a3a1a"},
    {"label": "W",        "command": "w",               "color": "#1a3a1a"},
    {"label": "U",        "command": "u",               "color": "#1a2a3a"},
    {"label": "D",        "command": "d",               "color": "#1a2a3a"},
]


@dataclass
class ButtonDef:
    label:   str
    command: str
    color:   str = "#2a2a3a"   # CSS hex colour for the button background


class _EditDialog(QDialog):
    """Simple dialog for editing a single ButtonDef."""

    def __init__(self, btn: ButtonDef, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Button")
        self._btn   = btn
        self._color = btn.color

        layout = QFormLayout(self)
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


class ButtonBar(QWidget):
    """
    Horizontal strip of macro buttons.

    Signals
    -------
    command_requested(str)  — emitted when a button is clicked; carry the
                              command string that should be sent to TinTin++
    """
    command_requested = pyqtSignal(str)

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

        self.load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, path: Path = _CONFIG_FILE):
        """Load button definitions from JSON, falling back to defaults."""
        try:
            with open(path) as f:
                raw = json.load(f)
            self._buttons = [ButtonDef(**b) for b in raw]
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            self._buttons = [ButtonDef(**b) for b in _DEFAULT_BUTTONS]
        self._rebuild()

    def save(self, path: Path = _CONFIG_FILE):
        """Persist button definitions to JSON."""
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump([asdict(b) for b in self._buttons], f, indent=2)

    def add_button(self, btn: ButtonDef = None):
        if btn is None:
            btn = ButtonDef(label="New", command="")
        self._buttons.append(btn)
        self._rebuild()
        self.save()

    # ------------------------------------------------------------------

    def _rebuild(self):
        """Destroy and recreate all button widgets from self._buttons."""
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
        add_act    = menu.addAction("Add button…")
        action = menu.exec(widget.mapToGlobal(pos))
        if action == edit_act:
            dlg = _EditDialog(self._buttons[idx], self)
            if dlg.exec():
                self._buttons[idx] = dlg.result_def()
                self._rebuild()
                self.save()
        elif action == delete_act:
            self._buttons.pop(idx)
            self._rebuild()
            self.save()
        elif action == add_act:
            dlg = _EditDialog(ButtonDef("New", ""), self)
            if dlg.exec():
                self._buttons.insert(idx + 1, dlg.result_def())
                self._rebuild()
                self.save()
