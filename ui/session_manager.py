"""
Session Manager — startup dialog for managing MUD connections.

Stored in ~/.config/tintin-gui/sessions.json as a list of session dicts:
  {
    "name":         "Toril",
    "host":         "torilmud.org",
    "port":         9999,
    "buttons":      [...],   ← per-session button definitions (list of dicts)
    "panel_layout": {...}
  }

The "buttons" field stores a list of button defs in the same format as
ButtonBar uses internally:  [{"label": "Look", "command": "look", "color": "#2a5a3a"}, ...]
An empty list means "use the ButtonBar defaults".

On Connect:
  1. TinTin++ is started (if not already running) loading the session
  2. The dialog issues:  #session {name} {host} {port}
  3. MainWindow updates its title and status bar.
"""

import json
from pathlib import Path
import dataclasses
from dataclasses import dataclass, asdict, field
from typing import List, Optional

from PyQt6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QListWidget,
    QListWidgetItem, QPushButton, QLabel, QLineEdit, QSpinBox,
    QFileDialog, QDialogButtonBox, QMessageBox, QFrame, QSplitter,
    QFormLayout, QAbstractItemView,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui  import QFont, QColor, QPalette


_CONFIG_DIR    = Path.home() / ".config" / "tintin-gui"
_SESSIONS_FILE = _CONFIG_DIR / "sessions.json"

_DARK = {
    "bg":       "#0d0d18",
    "panel":    "#12121e",
    "border":   "#2a2a40",
    "text":     "#ccccdd",
    "accent":   "#3a6aaa",
    "btn":      "#1e2a3a",
    "btn_hover":"#2a3a4a",
    "green":    "#2a5a2a",
    "red":      "#5a2a2a",
}

_FIELD_STYLE = f"""
QLineEdit, QSpinBox {{
    background: {_DARK['bg']};
    color: {_DARK['text']};
    border: 1px solid {_DARK['border']};
    border-radius: 3px;
    padding: 4px 6px;
}}
QLineEdit:focus, QSpinBox:focus {{
    border: 1px solid {_DARK['accent']};
}}
"""

_BTN_STYLE = f"""
QPushButton {{
    background: {_DARK['btn']};
    color: {_DARK['text']};
    border: 1px solid {_DARK['border']};
    border-radius: 3px;
    padding: 5px 14px;
}}
QPushButton:hover   {{ background: {_DARK['btn_hover']}; }}
QPushButton:pressed {{ background: {_DARK['bg']}; }}
QPushButton:disabled {{ color: #555; }}
"""

_CONNECT_STYLE = f"""
QPushButton {{
    background: {_DARK['green']};
    color: #aaffaa;
    border: 1px solid #3a7a3a;
    border-radius: 3px;
    padding: 6px 20px;
    font-weight: bold;
}}
QPushButton:hover   {{ background: #3a7a3a; }}
QPushButton:pressed {{ background: #1a3a1a; }}
QPushButton:disabled {{ background: #222; color: #555; border-color: #333; }}
"""

_DELETE_STYLE = f"""
QPushButton {{
    background: {_DARK['red']};
    color: #ffaaaa;
    border: 1px solid #7a3a3a;
    border-radius: 3px;
    padding: 5px 14px;
}}
QPushButton:hover   {{ background: #7a3a3a; }}
QPushButton:pressed {{ background: #3a1a1a; }}
"""


@dataclass
class Session:
    name:         str
    host:         str
    port:         int
    script:       str  = ""    # path to a .tin file loaded on connect
    buttons:      list = field(default_factory=list)
    aliases:      list = field(default_factory=list)
    actions:      list = field(default_factory=list)
    timers:       list = field(default_factory=list)
    highlights:   list = field(default_factory=list)
    variables:    list = field(default_factory=list)
    panel_layout: dict = field(default_factory=dict)
    font_size:    int  = 0   # 0 = use application default (currently 11pt)

    def display(self) -> str:
        script_tag = "  📄" if self.script else ""
        return f"{self.name}  —  {self.host}:{self.port}{script_tag}"


def _load_sessions() -> List[Session]:
    # Known Session field names — filter JSON dicts to only these so that
    # old, new, or otherwise mismatched files never cause Session(**s) to
    # raise TypeError and silently swallow all saved data.
    _SESSION_FIELDS = {f.name for f in dataclasses.fields(Session)}

    try:
        raw = json.loads(_SESSIONS_FILE.read_text())
        sessions = []
        for s in raw:
            s.setdefault("script",       "")
            s.setdefault("buttons",      [])
            s.setdefault("aliases",      [])
            s.setdefault("actions",      [])
            s.setdefault("timers",       [])
            s.setdefault("highlights",   [])
            s.setdefault("variables",    [])
            s.setdefault("panel_layout", {})
            s.setdefault("font_size",    0)
            # Drop any keys not in the current dataclass so unknown fields
            # from old/future versions never blow up Session(**s)
            s = {k: v for k, v in s.items() if k in _SESSION_FIELDS}
            sessions.append(Session(**s))
        return sessions
    except Exception:
        import traceback
        traceback.print_exc()
        return []


def _save_sessions(sessions: List[Session]):
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _SESSIONS_FILE.write_text(json.dumps([asdict(s) for s in sessions], indent=2))


class _SessionEditor(QDialog):
    """Create or edit a single session."""

    def __init__(self, session: Optional[Session] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Session" if session else "New Session")
        self.setMinimumWidth(440)
        self.setStyleSheet(f"background: {_DARK['panel']}; color: {_DARK['text']};")

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(18, 18, 18, 18)

        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        lbl_font = QFont()
        lbl_font.setPointSize(10)

        def lbl(text):
            l = QLabel(text)
            l.setFont(lbl_font)
            l.setStyleSheet(f"color: {_DARK['text']};")
            return l

        self._name   = QLineEdit(session.name   if session else "")
        self._host   = QLineEdit(session.host   if session else "")
        self._port   = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(session.port if session else 23)
        self._script = QLineEdit(session.script if session else "")
        self._script.setPlaceholderText("Optional — leave blank for none")

        for w in (self._name, self._host, self._script):
            w.setStyleSheet(_FIELD_STYLE)
        self._port.setStyleSheet(_FIELD_STYLE)

        browse = QPushButton("Browse…")
        browse.setStyleSheet(_BTN_STYLE)
        browse.clicked.connect(self._browse_script)

        script_row = QHBoxLayout()
        script_row.addWidget(self._script)
        script_row.addWidget(browse)

        form.addRow(lbl("Session name:"), self._name)
        form.addRow(lbl("Host:"),         self._host)
        form.addRow(lbl("Port:"),         self._port)
        form.addRow(lbl("TinTin script:"), script_row)

        layout.addLayout(form)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_DARK['border']};")
        layout.addWidget(sep)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.setStyleSheet(_BTN_STYLE)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_script(self):
        from pathlib import Path
        path, _ = QFileDialog.getOpenFileName(
            self, "Select TinTin++ script",
            str(Path.home()),
            "TinTin++ scripts (*.tin);;All files (*)"
        )
        if path:
            self._script.setText(path)

    def _validate_and_accept(self):
        if not self._name.text().strip():
            QMessageBox.warning(self, "Validation", "Session name is required.")
            return
        if not self._host.text().strip():
            QMessageBox.warning(self, "Validation", "Host is required.")
            return
        self.accept()

    def result_session(self, existing: Optional[Session] = None) -> Session:
        """
        Return a new Session, preserving all runtime fields from existing.
        The editor only touches name/host/port — everything else
        (buttons, aliases, actions, timers, highlights, panel_layout,
        font_size) is carried over unchanged.
        """
        return Session(
            name         = self._name.text().strip(),
            host         = self._host.text().strip(),
            port         = self._port.value(),
            script       = self._script.text().strip(),
            # Preserve all runtime/config fields from existing session
            buttons      = existing.buttons      if existing else [],
            aliases      = existing.aliases      if existing else [],
            actions      = existing.actions      if existing else [],
            timers       = existing.timers       if existing else [],
            highlights   = existing.highlights   if existing else [],
            variables    = existing.variables    if existing else [],
            panel_layout = existing.panel_layout if existing else {},
            font_size    = existing.font_size    if existing else 0,
        )


class _SessionList(QListWidget):
    """QListWidget that fires Enter/Return as a connect action."""
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            p = self.parent()
            while p:
                if isinstance(p, SessionManager):
                    p._on_connect()
                    return
                p = p.parent()
        super().keyPressEvent(event)


class SessionManager(QDialog):
    """
    Startup session picker.

    Signals
    -------
    connect_requested(Session)  — emitted when user clicks Connect
    """
    connect_requested = pyqtSignal(object)   # Session dataclass

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TinTin++ GUI — Sessions")
        self.setMinimumSize(580, 380)
        self.setStyleSheet(f"""
            QDialog    {{ background: {_DARK['panel']}; }}
            QLabel     {{ color: {_DARK['text']}; }}
            QListWidget {{
                background: {_DARK['bg']};
                color: {_DARK['text']};
                border: 1px solid {_DARK['border']};
                border-radius: 3px;
                font-size: 12px;
            }}
            QListWidget::item:selected {{
                background: {_DARK['accent']};
                color: #fff;
            }}
            QListWidget::item:hover {{
                background: #1e2a3a;
            }}
        """)

        self._sessions: List[Session] = _load_sessions()

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        title = QLabel("Select a session to connect")
        title.setStyleSheet(f"color: {_DARK['text']}; font-size: 13px; font-weight: bold;")
        root.addWidget(title)

        mid = QHBoxLayout()
        mid.setSpacing(10)

        self._list = _SessionList(self)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.itemDoubleClicked.connect(self._on_connect)
        self._list.currentRowChanged.connect(self._on_selection_change)
        mid.addWidget(self._list, stretch=1)

        side = QVBoxLayout()
        side.setSpacing(6)
        self._btn_new    = QPushButton("New…")
        self._btn_edit   = QPushButton("Edit…")
        self._btn_delete = QPushButton("Delete")
        self._btn_new.setStyleSheet(_BTN_STYLE)
        self._btn_edit.setStyleSheet(_BTN_STYLE)
        self._btn_delete.setStyleSheet(_DELETE_STYLE)
        self._btn_new.clicked.connect(self._on_new)
        self._btn_edit.clicked.connect(self._on_edit)
        self._btn_delete.clicked.connect(self._on_delete)
        side.addWidget(self._btn_new)
        side.addWidget(self._btn_edit)
        side.addWidget(self._btn_delete)
        side.addStretch()
        mid.addLayout(side)

        root.addLayout(mid, stretch=1)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_DARK['border']};")
        root.addWidget(sep)

        bottom = QHBoxLayout()
        self._btn_connect = QPushButton("Connect")
        self._btn_connect.setStyleSheet(_CONNECT_STYLE)
        self._btn_connect.setEnabled(False)
        self._btn_connect.setDefault(True)
        self._btn_connect.clicked.connect(self._on_connect)

        self._btn_cancel = QPushButton("Launch without connecting")
        self._btn_cancel.setStyleSheet(_BTN_STYLE)
        self._btn_cancel.clicked.connect(self.reject)

        bottom.addWidget(self._btn_cancel)
        bottom.addStretch()
        bottom.addWidget(self._btn_connect)
        root.addLayout(bottom)

        self._rebuild_list()

    def _rebuild_list(self):
        current = self._list.currentRow()
        self._list.clear()
        for s in self._sessions:
            item = QListWidgetItem(s.display())
            item.setData(Qt.ItemDataRole.UserRole, s)
            self._list.addItem(item)
        if self._sessions:
            row = max(0, min(current, len(self._sessions) - 1))
            self._list.setCurrentRow(row)
        self._on_selection_change(self._list.currentRow())

    def _on_selection_change(self, row: int):
        has = 0 <= row < len(self._sessions)
        self._btn_connect.setEnabled(has)
        self._btn_edit.setEnabled(has)
        self._btn_delete.setEnabled(has)

    def _current_session(self) -> Optional[Session]:
        row = self._list.currentRow()
        if 0 <= row < len(self._sessions):
            return self._sessions[row]
        return None

    def _on_new(self):
        dlg = _SessionEditor(parent=self)
        if dlg.exec():
            self._sessions.append(dlg.result_session())
            _save_sessions(self._sessions)
            self._rebuild_list()
            self._list.setCurrentRow(len(self._sessions) - 1)

    def _on_edit(self):
        s = self._current_session()
        if not s:
            return
        row = self._list.currentRow()
        dlg = _SessionEditor(session=s, parent=self)
        if dlg.exec():
            # Pass existing session so buttons/panel_layout are preserved
            self._sessions[row] = dlg.result_session(existing=s)
            _save_sessions(self._sessions)
            self._rebuild_list()
            self._list.setCurrentRow(row)

    def _on_delete(self):
        s = self._current_session()
        if not s:
            return
        ans = QMessageBox.question(
            self, "Delete session",
            f"Delete session '{s.name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            row = self._list.currentRow()
            self._sessions.pop(row)
            _save_sessions(self._sessions)
            self._rebuild_list()

    def _on_connect(self):
        s = self._current_session()
        if not s:
            return
        self.connect_requested.emit(s)
        self.accept()
