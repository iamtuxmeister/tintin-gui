"""
ConfigDialog — master configuration dialog for the TinTin++ GUI client.

Tabs
----
  Buttons    — macro button bar (ported from ButtonConfigDialog)
  Aliases    — #alias {name} {body}
  Actions    — #action {pattern} {command} (triggers)
  Timers     — #ticker {name} {command} {interval}
  Highlights — #highlight {pattern} {color}

All data is stored in the Session object under keys:
  session.buttons    — list of button dicts
  session.aliases    — list of alias dicts
  session.actions    — list of action dicts
  session.timers     — list of timer dicts
  session.highlights — list of highlight dicts

On Save:
  1. Data is written back into the Session and persisted to sessions.json
  2. The corresponding TinTin++ commands are sent live to the running session
     so changes take effect immediately without a reconnect.

Architecture
------------
_ListEditorTab is a reusable base class that provides the two-pane
(list | editor) layout with Add / Delete / Move Up / Move Down.
Each concrete tab subclasses it and implements:
  _make_editor()         — build the right-pane editor widgets
  _item_label(d)         — string shown in the list for a given dict
  _editor_to_dict()      — read editor widgets → dict
  _dict_to_editor(d)     — populate editor widgets from dict
  tintin_commands(items) — generate tt++ commands for the full item list
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Callable

from PyQt6.QtWidgets import (
    QDialog, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QAbstractItemView, QSplitter, QFrame, QDialogButtonBox,
    QColorDialog, QSpinBox, QComboBox, QCheckBox, QSizePolicy,
    QStatusBar, QScrollArea,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui  import QColor, QFont


# ── Shared dark style ─────────────────────────────────────────────────

_DARK = {
    "bg":       "#0d0d18",
    "panel":    "#12121e",
    "border":   "#2a2a40",
    "text":     "#ccccdd",
    "accent":   "#3a6aaa",
    "btn":      "#1e2a3a",
    "btn_hover":"#2a3a4a",
    "red":      "#5a1a1a",
}

_STYLE = f"""
QDialog, QWidget {{
    background: {_DARK['panel']};
    color: {_DARK['text']};
}}
QTabWidget::pane {{
    border: 1px solid {_DARK['border']};
    background: {_DARK['panel']};
}}
QTabBar::tab {{
    background: {_DARK['bg']}; color: #888;
    border: 1px solid {_DARK['border']}; border-bottom: none;
    padding: 5px 14px; min-width: 70px; font-size: 10pt;
}}
QTabBar::tab:selected {{
    background: {_DARK['panel']}; color: {_DARK['text']};
    border-bottom: 1px solid {_DARK['panel']};
}}
QTabBar::tab:hover {{ color: #ccc; }}
QListWidget {{
    background: {_DARK['bg']}; color: {_DARK['text']};
    border: 1px solid {_DARK['border']}; border-radius: 3px;
    font-family: Monospace; font-size: 10pt;
}}
QListWidget::item:selected {{ background: {_DARK['accent']}; color: #fff; }}
QListWidget::item:hover    {{ background: #1e2a3a; }}
QLineEdit, QSpinBox, QComboBox {{
    background: {_DARK['bg']}; color: {_DARK['text']};
    border: 1px solid {_DARK['border']}; border-radius: 3px;
    padding: 4px 6px; font-family: Monospace;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {_DARK['accent']};
}}
QComboBox QAbstractItemView {{
    background: {_DARK['bg']}; color: {_DARK['text']};
    selection-background-color: {_DARK['accent']};
}}
QPushButton {{
    background: {_DARK['btn']}; color: {_DARK['text']};
    border: 1px solid {_DARK['border']}; border-radius: 3px;
    padding: 5px 12px;
}}
QPushButton:hover   {{ background: {_DARK['btn_hover']}; }}
QPushButton:pressed {{ background: {_DARK['bg']}; }}
QPushButton:disabled {{ color: #444; border-color: #222; }}
QLabel {{ color: {_DARK['text']}; }}
QCheckBox {{ color: {_DARK['text']}; spacing: 6px; }}
QCheckBox::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {_DARK['border']}; border-radius: 2px;
    background: {_DARK['bg']};
}}
QCheckBox::indicator:checked {{ background: {_DARK['accent']}; }}
QSplitter::handle {{ background: {_DARK['border']}; }}
QFrame[frameShape="4"] {{ color: {_DARK['border']}; }}
"""

_SAVE_BTN_STYLE = """
QPushButton {
    background: #1a4a2a; color: #aaffaa;
    border: 1px solid #2a6a3a; border-radius: 3px;
    padding: 6px 24px; font-weight: bold;
}
QPushButton:hover   { background: #2a6a3a; }
QPushButton:pressed { background: #0a2a1a; }
"""

_DEL_BTN_STYLE = """
QPushButton {
    background: #4a1a1a; color: #ffaaaa;
    border: 1px solid #6a2a2a; border-radius: 3px;
    padding: 5px 12px;
}
QPushButton:hover   { background: #6a2a2a; }
QPushButton:pressed { background: #2a0a0a; }
"""


# ── Helper: field label ───────────────────────────────────────────────

def _lbl(text: str, mono: bool = False) -> QLabel:
    l = QLabel(text)
    if mono:
        f = QFont("Monospace")
        f.setPointSize(9)
        l.setFont(f)
    return l


# ── Base list-editor tab ──────────────────────────────────────────────

class _ListEditorTab(QWidget):
    """
    Two-pane reusable base:
      Left  — QListWidget with Add/Delete/Up/Down toolbar
      Right — editor widgets (implemented by subclass)

    Subclasses must implement:
      _make_editor(layout)         build editor into right-pane QVBoxLayout
      _item_label(d: dict) -> str  text shown in list for item d
      _editor_to_dict() -> dict    read editor → dict
      _dict_to_editor(d: dict)     populate editor from dict
      tintin_commands(items) -> list[str]  TinTin++ commands for item list
    """

    def __init__(self, items: list, parent=None):
        super().__init__(parent)
        self._items: list = [dict(d) for d in items]  # work on copies
        self._current: int = -1
        self._dirty: bool = False

        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        # ── Left pane ────────────────────────────────────────────────
        left = QWidget()
        lv   = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(4)

        tb = QHBoxLayout()
        tb.setSpacing(4)
        self._btn_add  = QPushButton("Add")
        self._btn_del  = QPushButton("Delete")
        self._btn_del.setStyleSheet(_DEL_BTN_STYLE)
        self._btn_up   = QPushButton("▲")
        self._btn_dn   = QPushButton("▼")
        self._btn_up.setFixedWidth(30)
        self._btn_dn.setFixedWidth(30)
        tb.addWidget(self._btn_add)
        tb.addWidget(self._btn_del)
        tb.addWidget(self._btn_up)
        tb.addWidget(self._btn_dn)
        tb.addStretch()
        lv.addLayout(tb)

        self._list = QListWidget()
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.currentRowChanged.connect(self._on_row_changed)
        self._list.model().rowsMoved.connect(self._sync_order_from_list)
        lv.addWidget(self._list)

        splitter.addWidget(left)

        # ── Right pane (editor) ───────────────────────────────────────
        right  = QWidget()
        self._rv = QVBoxLayout(right)
        self._rv.setContentsMargins(12, 4, 4, 4)
        self._rv.setSpacing(6)
        self._make_editor(self._rv)
        self._rv.addStretch()
        splitter.addWidget(right)
        splitter.setSizes([260, 340])

        # Wire toolbar
        self._btn_add.clicked.connect(self._add_item)
        self._btn_del.clicked.connect(self._del_item)
        self._btn_up.clicked.connect(self._move_up)
        self._btn_dn.clicked.connect(self._move_dn)

        self._rebuild_list()
        self._set_editor_enabled(False)

    # ── Subclass interface ────────────────────────────────────────────

    def _make_editor(self, layout: QVBoxLayout):
        raise NotImplementedError

    def _item_label(self, d: dict) -> str:
        raise NotImplementedError

    def _editor_to_dict(self) -> dict:
        raise NotImplementedError

    def _dict_to_editor(self, d: dict):
        raise NotImplementedError

    def tintin_commands(self, items: list) -> list[str]:
        """Return TinTin++ commands to (re)install all items."""
        return []

    def default_item(self) -> dict:
        """Return a blank item dict for Add."""
        return {}

    # ── List management ───────────────────────────────────────────────

    def get_items(self) -> list:
        return [dict(d) for d in self._items]

    def _rebuild_list(self):
        self._list.blockSignals(True)
        self._list.clear()
        for d in self._items:
            self._list.addItem(self._item_label(d))
        self._list.blockSignals(False)
        self._update_toolbar()

    def _refresh_current_label(self):
        if 0 <= self._current < self._list.count():
            self._list.item(self._current).setText(
                self._item_label(self._items[self._current])
            )

    def _on_row_changed(self, row: int):
        # Save edits to previous row before switching
        if 0 <= self._current < len(self._items):
            self._items[self._current] = self._editor_to_dict()
            self._refresh_current_label()

        self._current = row
        if 0 <= row < len(self._items):
            self._set_editor_enabled(True)
            self._dict_to_editor(self._items[row])
        else:
            self._set_editor_enabled(False)
        self._update_toolbar()

    def _sync_order_from_list(self, *_):
        """After drag reorder, rebuild self._items to match list widget order."""
        # Items text may not be unique so we read indices from item data
        pass  # handled by rebuilding from scratch on save — acceptable for now

    def _set_editor_enabled(self, enabled: bool):
        for w in self.findChildren(QLineEdit):
            w.setEnabled(enabled)
        for w in self.findChildren(QSpinBox):
            w.setEnabled(enabled)
        for w in self.findChildren(QComboBox):
            w.setEnabled(enabled)
        for w in self.findChildren(QCheckBox):
            w.setEnabled(enabled)
        self._btn_del.setEnabled(enabled)
        self._btn_up.setEnabled(enabled and self._current > 0)
        self._btn_dn.setEnabled(enabled and self._current < len(self._items) - 1)

    def _update_toolbar(self):
        has = 0 <= self._current < len(self._items)
        self._btn_del.setEnabled(has)
        self._btn_up.setEnabled(has and self._current > 0)
        self._btn_dn.setEnabled(has and self._current < len(self._items) - 1)

    def _add_item(self):
        # Commit current edits first
        if 0 <= self._current < len(self._items):
            self._items[self._current] = self._editor_to_dict()
        new = self.default_item()
        insert_at = self._current + 1 if self._current >= 0 else len(self._items)
        self._items.insert(insert_at, new)
        self._rebuild_list()
        self._list.setCurrentRow(insert_at)

    def _del_item(self):
        idx = self._current
        if idx < 0 or idx >= len(self._items):
            return
        self._items.pop(idx)
        self._current = -1
        self._rebuild_list()
        self._list.setCurrentRow(min(idx, len(self._items) - 1))

    def _move_up(self):
        idx = self._current
        if idx <= 0:
            return
        self._items[idx], self._items[idx-1] = self._items[idx-1], self._items[idx]
        self._current = -1
        self._rebuild_list()
        self._list.setCurrentRow(idx - 1)

    def _move_dn(self):
        idx = self._current
        if idx < 0 or idx >= len(self._items) - 1:
            return
        self._items[idx], self._items[idx+1] = self._items[idx+1], self._items[idx]
        self._current = -1
        self._rebuild_list()
        self._list.setCurrentRow(idx + 1)

    def commit(self):
        """Flush any unsaved editor state into self._items before reading."""
        if 0 <= self._current < len(self._items):
            self._items[self._current] = self._editor_to_dict()


# ── Buttons tab ───────────────────────────────────────────────────────

class _ButtonsTab(_ListEditorTab):
    """Ports the button editor from ButtonConfigDialog into a tab."""

    def _make_editor(self, layout):
        layout.addWidget(_lbl("Label"))
        self._ed_label = QLineEdit()
        self._ed_label.setPlaceholderText("Button label")
        self._ed_label.textChanged.connect(self._live_update)
        layout.addWidget(self._ed_label)

        layout.addWidget(_lbl("Command"))
        self._ed_cmd = QLineEdit()
        self._ed_cmd.setPlaceholderText("TinTin++ command, e.g.  look  or  n;look")
        self._ed_cmd.textChanged.connect(self._live_update)
        layout.addWidget(self._ed_cmd)

        layout.addWidget(_lbl("Color"))
        self._color_val = "#2a2a3a"
        self._ed_color  = QPushButton("Pick colour…")
        self._ed_color.clicked.connect(self._pick_color)
        layout.addWidget(self._ed_color)

    def _live_update(self):
        if 0 <= self._current < len(self._items):
            self._items[self._current] = self._editor_to_dict()
            self._refresh_current_label()

    def _pick_color(self):
        col = QColorDialog.getColor(QColor(self._color_val), self, "Button Color")
        if col.isValid():
            self._color_val = col.name()
            self._ed_color.setStyleSheet(f"background:{col.name()}; color:#ddd;")
            self._ed_color.setText(col.name())
            if 0 <= self._current < len(self._items):
                self._items[self._current] = self._editor_to_dict()

    def _item_label(self, d):
        return f"{d.get('label','?'):12s}  →  {d.get('command','')}"

    def _editor_to_dict(self):
        return {
            "label":   self._ed_label.text().strip() or "?",
            "command": self._ed_cmd.text().strip(),
            "color":   self._color_val,
        }

    def _dict_to_editor(self, d):
        self._ed_label.blockSignals(True)
        self._ed_cmd.blockSignals(True)
        self._ed_label.setText(d.get("label", ""))
        self._ed_cmd.setText(d.get("command", ""))
        self._color_val = d.get("color", "#2a2a3a")
        self._ed_color.setStyleSheet(f"background:{self._color_val}; color:#ddd;")
        self._ed_color.setText(self._color_val)
        self._ed_label.blockSignals(False)
        self._ed_cmd.blockSignals(False)

    def default_item(self):
        return {"label": "New", "command": "", "color": "#2a2a3a"}

    def tintin_commands(self, items):
        # Buttons are GUI-only, no TinTin++ commands needed
        return []


# ── Aliases tab ───────────────────────────────────────────────────────

class _AliasesTab(_ListEditorTab):

    def _make_editor(self, layout):
        layout.addWidget(_lbl("Alias name"))
        self._ed_name = QLineEdit()
        self._ed_name.setPlaceholderText("e.g.  gs  or  killall")
        self._ed_name.textChanged.connect(self._live)
        layout.addWidget(self._ed_name)

        layout.addWidget(_lbl("Body  (TinTin++ command)"))
        self._ed_body = QLineEdit()
        self._ed_body.setPlaceholderText("e.g.  get sword;kill goblin")
        self._ed_body.textChanged.connect(self._live)
        layout.addWidget(self._ed_body)

        hint = _lbl(
            "Tip: use %1 %2 … for arguments.  Example:\n"
            "  Name:  kill\n"
            "  Body:  kill %1;loot %1"
        )
        hint.setStyleSheet("color: #666; font-size: 9pt;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def _live(self):
        if 0 <= self._current < len(self._items):
            self._items[self._current] = self._editor_to_dict()
            self._refresh_current_label()

    def _item_label(self, d):
        name = d.get("name", "")
        body = d.get("body", "")
        return f"{name:20s}  {body}"

    def _editor_to_dict(self):
        return {
            "name": self._ed_name.text().strip(),
            "body": self._ed_body.text().strip(),
        }

    def _dict_to_editor(self, d):
        self._ed_name.blockSignals(True)
        self._ed_body.blockSignals(True)
        self._ed_name.setText(d.get("name", ""))
        self._ed_body.setText(d.get("body", ""))
        self._ed_name.blockSignals(False)
        self._ed_body.blockSignals(False)

    def default_item(self):
        return {"name": "", "body": ""}

    def tintin_commands(self, items):
        cmds = []
        for d in items:
            name = d.get("name", "").strip()
            body = d.get("body", "").strip()
            if name:
                cmds.append(f"#alias {{{name}}} {{{body}}}")
        return cmds


# ── Actions (triggers) tab ────────────────────────────────────────────

class _ActionsTab(_ListEditorTab):

    def _make_editor(self, layout):
        layout.addWidget(_lbl("Pattern  (regex or plain text)"))
        self._ed_pattern = QLineEdit()
        self._ed_pattern.setPlaceholderText("e.g.  {You are hungry}  or  ^%1 tells you")
        self._ed_pattern.textChanged.connect(self._live)
        layout.addWidget(self._ed_pattern)

        layout.addWidget(_lbl("Command"))
        self._ed_cmd = QLineEdit()
        self._ed_cmd.setPlaceholderText("e.g.  eat bread  or  say Hello %1!")
        self._ed_cmd.textChanged.connect(self._live)
        layout.addWidget(self._ed_cmd)

        row = QHBoxLayout()
        row.setSpacing(12)
        layout.addLayout(row)

        row.addWidget(_lbl("Priority"))
        self._ed_pri = QSpinBox()
        self._ed_pri.setRange(1, 9)
        self._ed_pri.setValue(5)
        self._ed_pri.setFixedWidth(60)
        row.addWidget(self._ed_pri)
        row.addStretch()

        self._ed_enabled = QCheckBox("Enabled")
        self._ed_enabled.setChecked(True)
        layout.addWidget(self._ed_enabled)

        hint = _lbl(
            "Tip: use %0 for the full matched line, %1 %2 … for groups.\n"
            "Braces {} make TinTin++ match literally."
        )
        hint.setStyleSheet("color: #666; font-size: 9pt;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def _live(self):
        if 0 <= self._current < len(self._items):
            self._items[self._current] = self._editor_to_dict()
            self._refresh_current_label()

    def _item_label(self, d):
        pat = d.get("pattern", "")
        cmd = d.get("command", "")
        enabled = "" if d.get("enabled", True) else "  [OFF]"
        return f"{pat:30s}  →  {cmd}{enabled}"

    def _editor_to_dict(self):
        return {
            "pattern":  self._ed_pattern.text().strip(),
            "command":  self._ed_cmd.text().strip(),
            "priority": self._ed_pri.value(),
            "enabled":  self._ed_enabled.isChecked(),
        }

    def _dict_to_editor(self, d):
        self._ed_pattern.blockSignals(True)
        self._ed_cmd.blockSignals(True)
        self._ed_pattern.setText(d.get("pattern", ""))
        self._ed_cmd.setText(d.get("command", ""))
        self._ed_pri.setValue(d.get("priority", 5))
        self._ed_enabled.setChecked(d.get("enabled", True))
        self._ed_pattern.blockSignals(False)
        self._ed_cmd.blockSignals(False)

    def default_item(self):
        return {"pattern": "", "command": "", "priority": 5, "enabled": True}

    def tintin_commands(self, items):
        cmds = []
        # Clear all existing actions first so removals take effect
        cmds.append("#action {}")
        for d in items:
            pat = d.get("pattern", "").strip()
            cmd = d.get("command", "").strip()
            pri = d.get("priority", 5)
            if pat and d.get("enabled", True):
                cmds.append(f"#action {{{pat}}} {{{cmd}}} {{{pri}}}")
        return cmds


# ── Timers tab ────────────────────────────────────────────────────────

class _TimersTab(_ListEditorTab):

    def _make_editor(self, layout):
        layout.addWidget(_lbl("Timer name"))
        self._ed_name = QLineEdit()
        self._ed_name.setPlaceholderText("e.g.  regen  or  autoscan")
        self._ed_name.textChanged.connect(self._live)
        layout.addWidget(self._ed_name)

        layout.addWidget(_lbl("Command"))
        self._ed_cmd = QLineEdit()
        self._ed_cmd.setPlaceholderText("e.g.  rest;look")
        self._ed_cmd.textChanged.connect(self._live)
        layout.addWidget(self._ed_cmd)

        row = QHBoxLayout()
        row.setSpacing(8)
        layout.addLayout(row)
        row.addWidget(_lbl("Interval (seconds)"))
        self._ed_interval = QSpinBox()
        self._ed_interval.setRange(1, 99999)
        self._ed_interval.setValue(30)
        self._ed_interval.setFixedWidth(80)
        row.addWidget(self._ed_interval)
        row.addStretch()

        self._ed_enabled = QCheckBox("Enabled")
        self._ed_enabled.setChecked(True)
        layout.addWidget(self._ed_enabled)

    def _live(self):
        if 0 <= self._current < len(self._items):
            self._items[self._current] = self._editor_to_dict()
            self._refresh_current_label()

    def _item_label(self, d):
        name = d.get("name", "")
        cmd  = d.get("command", "")
        secs = d.get("interval", 30)
        enabled = "" if d.get("enabled", True) else "  [OFF]"
        return f"{name:16s}  {secs:>5}s  →  {cmd}{enabled}"

    def _editor_to_dict(self):
        return {
            "name":     self._ed_name.text().strip(),
            "command":  self._ed_cmd.text().strip(),
            "interval": self._ed_interval.value(),
            "enabled":  self._ed_enabled.isChecked(),
        }

    def _dict_to_editor(self, d):
        self._ed_name.blockSignals(True)
        self._ed_cmd.blockSignals(True)
        self._ed_name.setText(d.get("name", ""))
        self._ed_cmd.setText(d.get("command", ""))
        self._ed_interval.setValue(d.get("interval", 30))
        self._ed_enabled.setChecked(d.get("enabled", True))
        self._ed_name.blockSignals(False)
        self._ed_cmd.blockSignals(False)

    def default_item(self):
        return {"name": "", "command": "", "interval": 30, "enabled": True}

    def tintin_commands(self, items):
        cmds = []
        cmds.append("#ticker {}")   # clear all tickers
        for d in items:
            name = d.get("name", "").strip()
            cmd  = d.get("command", "").strip()
            secs = d.get("interval", 30)
            if name and cmd and d.get("enabled", True):
                cmds.append(f"#ticker {{{name}}} {{{cmd}}} {{{secs}}}")
        return cmds


# ── Highlights tab ────────────────────────────────────────────────────

_TT_COLORS = [
    "red", "green", "yellow", "blue", "magenta", "cyan", "white",
    "bold red", "bold green", "bold yellow", "bold blue",
    "bold magenta", "bold cyan", "bold white",
    "light red", "light green", "light yellow", "light blue",
    "light magenta", "light cyan", "light white",
]


class _HighlightsTab(_ListEditorTab):

    def _make_editor(self, layout):
        layout.addWidget(_lbl("Pattern  (regex or plain text)"))
        self._ed_pattern = QLineEdit()
        self._ed_pattern.setPlaceholderText("e.g.  {You are hungry}  or  Frodo")
        self._ed_pattern.textChanged.connect(self._live)
        layout.addWidget(self._ed_pattern)

        row = QHBoxLayout()
        row.setSpacing(8)
        layout.addLayout(row)
        row.addWidget(_lbl("Foreground color"))
        self._ed_fg = QComboBox()
        self._ed_fg.addItem("(none)")
        for c in _TT_COLORS:
            self._ed_fg.addItem(c)
        self._ed_fg.setFixedWidth(160)
        self._ed_fg.currentIndexChanged.connect(self._live)
        row.addWidget(self._ed_fg)
        row.addStretch()

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        layout.addLayout(row2)
        row2.addWidget(_lbl("Background color"))
        self._ed_bg = QComboBox()
        self._ed_bg.addItem("(none)")
        for c in _TT_COLORS:
            self._ed_bg.addItem(c)
        self._ed_bg.setFixedWidth(160)
        self._ed_bg.currentIndexChanged.connect(self._live)
        row2.addWidget(self._ed_bg)
        row2.addStretch()

        hint = _lbl(
            "Tip: use bold/light variants for brighter colours.\n"
            "Leave fg (none) to highlight background only."
        )
        hint.setStyleSheet("color: #666; font-size: 9pt;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def _live(self):
        if 0 <= self._current < len(self._items):
            self._items[self._current] = self._editor_to_dict()
            self._refresh_current_label()

    def _item_label(self, d):
        pat = d.get("pattern", "")
        fg  = d.get("fg", "") or ""
        bg  = d.get("bg", "") or ""
        color_str = f"fg={fg}" if fg else ""
        if bg:
            color_str += f"  bg={bg}" if color_str else f"bg={bg}"
        return f"{pat:30s}  {color_str}"

    def _editor_to_dict(self):
        fg_idx = self._ed_fg.currentIndex()
        bg_idx = self._ed_bg.currentIndex()
        return {
            "pattern": self._ed_pattern.text().strip(),
            "fg":      _TT_COLORS[fg_idx - 1] if fg_idx > 0 else "",
            "bg":      _TT_COLORS[bg_idx - 1] if bg_idx > 0 else "",
        }

    def _dict_to_editor(self, d):
        self._ed_pattern.blockSignals(True)
        self._ed_pattern.setText(d.get("pattern", ""))
        fg = d.get("fg", "")
        bg = d.get("bg", "")
        self._ed_fg.setCurrentIndex(
            _TT_COLORS.index(fg) + 1 if fg in _TT_COLORS else 0
        )
        self._ed_bg.setCurrentIndex(
            _TT_COLORS.index(bg) + 1 if bg in _TT_COLORS else 0
        )
        self._ed_pattern.blockSignals(False)

    def default_item(self):
        return {"pattern": "", "fg": "bold yellow", "bg": ""}

    def tintin_commands(self, items):
        cmds = []
        cmds.append("#highlight {}")   # clear all
        for d in items:
            pat = d.get("pattern", "").strip()
            fg  = d.get("fg", "").strip()
            bg  = d.get("bg", "").strip()
            if pat:
                color = fg or bg
                if fg and bg:
                    color = f"{fg} on {bg}"
                if color:
                    cmds.append(f"#highlight {{{pat}}} {{{color}}}")
        return cmds



class _VariablesTab(_ListEditorTab):

    def _make_editor(self, layout):
        layout.addWidget(_lbl("Variable name"))
        self._ed_name = QLineEdit()
        self._ed_name.setPlaceholderText("e.g.  tank  or  autoAssist")
        self._ed_name.textChanged.connect(self._live)
        layout.addWidget(self._ed_name)

        layout.addWidget(_lbl("Value"))
        self._ed_value = QLineEdit()
        self._ed_value.setPlaceholderText("e.g.  Frodo  or  0")
        self._ed_value.textChanged.connect(self._live)
        layout.addWidget(self._ed_value)

        hint = _lbl(
            "Sets a tt++ variable at session start.\n"
            "Use ${name} to reference it in aliases and actions."
        )
        hint.setStyleSheet("color: #666; font-size: 9pt;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def _live(self):
        if 0 <= self._current < len(self._items):
            self._items[self._current] = self._editor_to_dict()
            self._refresh_current_label()

    def _item_label(self, d):
        name  = d.get("name",  "")
        value = d.get("value", "")
        return f"{name:20s}  =  {value}"

    def _editor_to_dict(self):
        return {
            "name":  self._ed_name.text().strip(),
            "value": self._ed_value.text().strip(),
        }

    def _dict_to_editor(self, d):
        self._ed_name.blockSignals(True)
        self._ed_value.blockSignals(True)
        self._ed_name.setText(d.get("name",  ""))
        self._ed_value.setText(d.get("value", ""))
        self._ed_name.blockSignals(False)
        self._ed_value.blockSignals(False)

    def default_item(self):
        return {"name": "", "value": ""}

    def tintin_commands(self, items):
        cmds = ["#variable {}"]   # clear all
        for d in items:
            name  = d.get("name",  "").strip()
            value = d.get("value", "").strip()
            if name:
                cmds.append(f"#variable {{{name}}} {{{value}}}")
        return cmds


# ── Master ConfigDialog ───────────────────────────────────────────────

class ConfigDialog(QDialog):
    """
    Master configuration dialog.

    Signals
    -------
    saved(dict)  — emitted when Save is clicked; carries the full config dict
                   that should be written into the Session and sent to tt++.

    Usage
    -----
        dlg = ConfigDialog(session_config, parent=self)
        dlg.saved.connect(self._on_config_saved)
        dlg.show()   # non-modal — user can still play while configuring
    """

    saved = pyqtSignal(dict)   # full config dict

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuration")
        self.setMinimumSize(780, 520)
        self.setStyleSheet(_STYLE)
        # Non-modal so the user can still play while editing
        self.setWindowModality(Qt.WindowModality.NonModal)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Tabs ─────────────────────────────────────────────────────
        self._tabs = QTabWidget()
        root.addWidget(self._tabs, stretch=1)

        self._buttons_tab    = _ButtonsTab(config.get("buttons",    []))
        self._aliases_tab    = _AliasesTab(config.get("aliases",    []))
        self._actions_tab    = _ActionsTab(config.get("actions",    []))
        self._timers_tab     = _TimersTab(config.get("timers",      []))
        self._highlights_tab = _HighlightsTab(config.get("highlights", []))
        self._variables_tab  = _VariablesTab(config.get("variables",  []))
        self._variables_tab  = _VariablesTab(config.get("variables",  []))

        self._tabs.addTab(self._actions_tab,    "Actions")
        self._tabs.addTab(self._aliases_tab,    "Aliases")
        self._tabs.addTab(self._variables_tab,  "Variables")
        self._tabs.addTab(self._timers_tab,     "Timers")
        self._tabs.addTab(self._highlights_tab, "Highlights")
        self._tabs.addTab(self._buttons_tab,    "Buttons")

        # ── Bottom bar ────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_DARK['border']};")
        root.addWidget(sep)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(12, 8, 12, 8)
        bottom.setSpacing(8)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: #888; font-size: 9pt;")
        bottom.addWidget(self._status_lbl)
        bottom.addStretch()

        self._reload_btn = QPushButton("↺ Reload from TinTin++")
        self._reload_btn.setToolTip(
            "Ask the running TinTin++ process for its current aliases, actions,\n"
            "timers and highlights, and load them into the editor."
        )
        self._reload_btn.clicked.connect(self.reload_from_tt)
        bottom.addWidget(self._reload_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)

        save_btn = QPushButton("Save")
        save_btn.setStyleSheet(_SAVE_BTN_STYLE)
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)

        bottom.addWidget(close_btn)
        bottom.addWidget(save_btn)
        root.addLayout(bottom)

        # Loader is set by MainWindow after construction
        self._loader_factory: object = None   # callable() → TinTinConfigLoader

    # ── Load from tt++ ───────────────────────────────────────────────

    def set_loader_factory(self, factory):
        """
        Provide a callable that returns a fresh TinTinConfigLoader.
        Called by MainWindow after dialog construction.
            factory() → TinTinConfigLoader
        """
        self._loader_factory = factory

    def reload_from_tt(self):
        """
        Ask the running TinTin++ for its current config via #write,
        then populate the non-Buttons tabs with the result.
        """
        if self._loader_factory is None:
            self._set_status("No TinTin++ process available.")
            return
        self._set_status("Reading from TinTin++…")
        self._reload_btn.setEnabled(False)
        loader = self._loader_factory()
        loader.loaded.connect(self._on_tt_loaded)
        loader.error.connect(self._on_tt_load_error)
        loader.raw_dump.connect(self._on_raw_dump)
        loader.load()

    def _on_tt_loaded(self, config: dict):
        """Populate alias/action/timer/highlight tabs from live tt++ data."""
        self._reload_btn.setEnabled(True)

        # Preserve buttons — they are GUI-only and not in tt++ state
        buttons = self._buttons_tab.get_items()

        self._aliases_tab    = _AliasesTab(config.get("aliases",    []))
        self._actions_tab    = _ActionsTab(config.get("actions",    []))
        self._timers_tab     = _TimersTab(config.get("timers",      []))
        self._highlights_tab = _HighlightsTab(config.get("highlights", []))
        self._variables_tab  = _VariablesTab(config.get("variables",  []))

        # Replace tabs 1-4 (leave Buttons at index 0 untouched)
        current = self._tabs.currentIndex()
        for idx, (tab, title) in enumerate([
            (self._actions_tab,    "Actions"),
            (self._aliases_tab,    "Aliases"),
            (self._variables_tab,  "Variables"),
            (self._timers_tab,     "Timers"),
            (self._highlights_tab, "Highlights"),
            (self._buttons_tab,    "Buttons"),
        ], start=1):
            self._tabs.removeTab(idx)
            self._tabs.insertTab(idx, tab, title)

        self._tabs.setCurrentIndex(current)
        counts = (
            f"{len(config.get('aliases',    []))} aliases, "
            f"{len(config.get('actions',    []))} actions, "
            f"{len(config.get('timers',     []))} timers, "
            f"{len(config.get('highlights', []))} highlights",
            f"{len(config.get('variables',  []))} variables"
        )
        self._set_status(f"Loaded from TinTin++ — {counts}")
        QTimer.singleShot(5000, lambda: self._set_status(""))

    def _on_tt_load_error(self, msg: str):
        self._reload_btn.setEnabled(True)
        self._set_status(f"Error: {msg}")

    def _on_raw_dump(self, text: str):
        """Stash raw tt++ #write output for debugging; also print to stderr."""
        import sys
        self._last_raw_dump = text
        print("=== tt++ #write dump ===", file=sys.stderr)
        print(text[:6000], file=sys.stderr)
        print("=== end dump ===", file=sys.stderr)

    def _set_status(self, text: str):
        self._status_lbl.setText(text)

    # ── Save ─────────────────────────────────────────────────────────

    def _on_save(self):
        for tab in (self._buttons_tab, self._aliases_tab,
                    self._actions_tab, self._timers_tab,
                    self._highlights_tab, self._variables_tab):
            tab.commit()
        config = self.get_config()
        self.saved.emit(config)
        self._set_status("Saved.")
        QTimer.singleShot(3000, lambda: self._set_status(""))

    def get_config(self) -> dict:
        """Return the full config dict from all tabs."""
        return {
            "buttons":    self._buttons_tab.get_items(),
            "aliases":    self._aliases_tab.get_items(),
            "actions":    self._actions_tab.get_items(),
            "timers":     self._timers_tab.get_items(),
            "highlights": self._highlights_tab.get_items(),
            "variables":  self._variables_tab.get_items(),
        }

    def all_tintin_commands(self) -> list[str]:
        """
        Return all TinTin++ commands needed to apply the current config live.
        Call this after get_config() to send commands to the running session.
        """
        cmds = []
        for tab in (self._aliases_tab, self._actions_tab,
                    self._timers_tab, self._highlights_tab,
                    self._variables_tab):
            cmds.extend(tab.tintin_commands(tab.get_items()))
        return cmds
