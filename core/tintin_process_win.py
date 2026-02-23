"""
TinTin++ process manager — Windows (WinTin++) edition.

Uses the pywinpty package which wraps Windows ConPTY API, giving us a
proper pseudo-terminal on Windows without needing pty/fcntl/termios.

WinTin++ (tt.exe) is installed by the official MSI installer from:
  https://tintin.mudhalla.net/download.php

Default install location: C:\\Program Files (x86)\\WinTin++\\bin\\tt.exe
We also check PATH in case the user added it there.

Install pywinpty with:  pip install pywinpty

Threading note
--------------
_reader() runs on a daemon background thread and emits output_received and
process_died signals directly from that thread.  This is intentional and
safe: PyQt6 automatically queues cross-thread signal emissions (equivalent
to Qt::QueuedConnection), so the connected slots always execute on the main
GUI thread on the next event-loop iteration.  No explicit locking is needed
because the only mutable state _reader() touches is self._pty.isalive() /
self._pty.read(), which are both safe to call from a non-GUI thread.
"""

import os
import shutil
import tempfile
import threading

from PyQt6.QtCore import QObject, pyqtSignal

try:
    import winpty
    _WINPTY_OK = True
except ImportError:
    _WINPTY_OK = False

_WINTIN_DEFAULT_PATHS = [
    r"C:\Program Files (x86)\WinTin++\bin\tt.exe",
    r"C:\Program Files\WinTin++\bin\tt.exe",
    r"C:\WinTin++\bin\tt.exe",
]

_INIT_SNIPPET = (
    "#config SCREENREADER ON\n"
    "#config MOUSE OFF\n"
    "#config BELL OFF\n"
    "#config PACKET PATCH 1\n"
)


def _find_wintin() -> str | None:
    # Check PATH first
    found = shutil.which("tt") or shutil.which("tt++")
    if found:
        return found
    # Check known install locations
    for p in _WINTIN_DEFAULT_PATHS:
        if os.path.isfile(p):
            return p
    return None


class TinTinProcess(QObject):
    """
    Windows implementation using pywinpty (ConPTY).

    Signals identical to the Linux version so MainWindow needs no changes.

    output_received(bytes) — emitted from the reader thread; PyQt6 queues it
                             automatically so slots run on the GUI thread.
    process_died(int)      — same threading contract as output_received.
    """
    output_received = pyqtSignal(bytes)
    process_died    = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pty        = None   # winpty.PtyProcess instance
        self._thread     = None   # reader thread
        self._running    = False
        self._tmp_script = None

    def start(self, script_path=None) -> bool:
        if not _WINPTY_OK:
            raise ImportError(
                "pywinpty is not installed.\n"
                "Install with:  pip install pywinpty"
            )

        tt_bin = _find_wintin()
        if tt_bin is None:
            raise FileNotFoundError(
                "WinTin++ (tt.exe) not found.\n"
                "Download the installer from https://tintin.mudhalla.net/download.php\n"
                "Default install path: C:\\Program Files (x86)\\WinTin++\\bin\\tt.exe"
            )

        # Write startup script to temp file
        self._tmp_script = tempfile.NamedTemporaryFile(
            mode="w", suffix=".tin", delete=False, prefix="ttgui_",
            encoding="utf-8",
        )
        self._tmp_script.write(_INIT_SNIPPET)
        if script_path:
            # Use forward slashes — tt++ handles them on Windows
            safe = script_path.replace("\\", "/")
            self._tmp_script.write(f"\n#read {{{safe}}}\n")
        self._tmp_script.flush()
        self._tmp_script.close()
        tmp_path = self._tmp_script.name.replace("\\", "/")

        # Launch via winpty (ConPTY)
        env = os.environ.copy()
        env["TERM"] = "dumb"

        self._pty = winpty.PtyProcess.spawn(
            [tt_bin, tmp_path],
            dimensions=(50, 220),
            env=env,
        )

        self._running = True
        # FIX 4: daemon=True so this thread never blocks clean process exit.
        # Signal emissions from here are automatically queued by PyQt6 to the
        # GUI thread — no explicit QueuedConnection or locking required.
        self._thread  = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        return True

    def send(self, text: str):
        self.send_raw((text + "\n").encode("utf-8", errors="replace"))

    def send_raw(self, data: bytes):
        if self._pty is None:
            return
        try:
            self._pty.write(data.decode("utf-8", errors="replace"))
        except Exception:
            pass

    def resize(self, cols: int, rows: int):
        if self._pty is None:
            return
        try:
            self._pty.setwinsize(rows, cols)
        except Exception:
            pass

    def stop(self):
        self._running = False
        if self._pty:
            try:
                self._pty.terminate(force=True)
            except Exception:
                pass
            self._pty = None
        if self._tmp_script:
            try:
                os.unlink(self._tmp_script.name)
            except OSError:
                pass
            self._tmp_script = None

    @property
    def running(self) -> bool:
        return self._pty is not None and self._pty.isalive()

    def _reader(self):
        """
        Background thread: read from ConPTY and emit signals.

        Runs until the PTY closes or self._running is cleared by stop().
        Signal emissions are cross-thread but safe — PyQt6 queues them.
        """
        while self._running and self._pty and self._pty.isalive():
            try:
                data = self._pty.read(8192)
                if data:
                    # Cross-thread emit — PyQt6 queues this automatically
                    self.output_received.emit(
                        data if isinstance(data, bytes)
                        else data.encode("utf-8", errors="replace")
                    )
            except Exception:
                break

        code = 0
        try:
            code = self._pty.exitstatus if self._pty else 0
        except Exception:
            pass
        # Cross-thread emit — PyQt6 queues this automatically
        self.process_died.emit(code or 0)
