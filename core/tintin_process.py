"""
TinTin++ process manager — subprocess + QSocketNotifier edition.

Key insight: tt++ is a full-screen TUI that uses cursor-positioning escape
codes to draw its interface.  We do NOT want that — we want plain line-
oriented output that our Qt widgets can render.  We achieve this by:

  1. Setting TERM=dumb in the child environment so tt++ knows it has no
     cursor-addressing capability.
  2. Injecting a tiny startup script via a temp file that sets:
       #config SCREENREADER ON   ← disables tt++'s own TUI layout
       #config MOUSE OFF         ← no mouse escape sequences
       #config BELL OFF          ← no terminal bell
     then chains to the user's real script (if any) with #read.

This makes tt++ behave as a pure line-oriented engine — aliases, triggers,
scripting, mapper data all still work; we just own the display entirely.
"""

import os
import pty
import fcntl
import termios
import struct
import shutil
import signal
import subprocess
import tempfile

from PyQt6.QtCore import QObject, pyqtSignal, QSocketNotifier


# Startup snippet injected before any user script
_INIT_SNIPPET = """\
#config SCREENREADER ON
#config MOUSE OFF
#config BELL OFF
#config PACKET PATCH 1
#config LOGMODE HTML
"""


class TinTinProcess(QObject):
    """
    Owns the TinTin++ child process and the PTY pair.

    Signals
    -------
    output_received(bytes)  -- raw bytes from tt++ (ANSI colours still intact)
    process_died(int)       -- tt++ exited with this code
    """
    output_received = pyqtSignal(bytes)
    process_died    = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.master_fd   = -1
        self._proc       = None
        self._notifier   = None
        self._tmp_script = None   # NamedTemporaryFile kept alive

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, script_path=None):
        tt_bin = shutil.which("tt++") or shutil.which("tintin++")
        if tt_bin is None:
            raise FileNotFoundError(
                "tt++ / tintin++ not found in PATH.\n"
                "Install with:  sudo apt install tintin++"
            )

        # Build init script in a temp file
        self._tmp_script = tempfile.NamedTemporaryFile(
            mode="w", suffix=".tin", delete=False, prefix="ttgui_"
        )
        self._tmp_script.write(_INIT_SNIPPET)
        if script_path:
            self._tmp_script.write(f"\n#read {{{script_path}}}\n")
        self._tmp_script.flush()
        self._tmp_script.close()

        # PTY pair
        self.master_fd, slave_fd = pty.openpty()
        self._set_winsize(self.master_fd, 220, 50)

        # Environment: TERM=dumb stops tt++ using cursor-address sequences
        env = os.environ.copy()
        env["TERM"] = "dumb"

        self._proc = subprocess.Popen(
            [tt_bin, self._tmp_script.name],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=os.setsid,
            env=env,
        )
        os.close(slave_fd)

        # Non-blocking reads
        flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
        fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        # Qt event-loop driven reader — no threads
        self._notifier = QSocketNotifier(
            self.master_fd,
            QSocketNotifier.Type.Read,
            self,
        )
        self._notifier.activated.connect(self._on_readable)
        return True

    def send(self, text):
        self.send_raw((text + "\n").encode("utf-8", errors="replace"))

    def send_raw(self, data):
        if self.master_fd < 0:
            return
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def resize(self, cols, rows):
        if self.master_fd < 0 or self._proc is None:
            return
        self._set_winsize(self.master_fd, cols, rows)
        try:
            os.kill(self._proc.pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass

    def stop(self):
        if self._notifier:
            self._notifier.setEnabled(False)
            self._notifier = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self.master_fd >= 0:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = -1
        # Clean up temp script
        if self._tmp_script:
            try:
                os.unlink(self._tmp_script.name)
            except OSError:
                pass
            self._tmp_script = None

    @property
    def running(self):
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------

    def _on_readable(self, fd):
        try:
            data = os.read(self.master_fd, 8192)
        except OSError:
            data = b""

        if data:
            self.output_received.emit(data)
        else:
            self._notifier.setEnabled(False)
            code = self._proc.wait() if self._proc else -1
            self.process_died.emit(code)

    @staticmethod
    def _set_winsize(fd, cols, rows):
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass
