"""
Platform dispatcher — import this instead of tintin_process directly.

    from core.tintin_process_compat import TinTinProcess

Automatically selects:
  - Windows  → tintin_process_win  (pywinpty + ConPTY + tt.exe)
  - Linux / macOS → tintin_process  (pty + subprocess + tt++)
"""
import platform

if platform.system() == "Windows":
    from core.tintin_process_win import TinTinProcess
else:
    from core.tintin_process import TinTinProcess

__all__ = ["TinTinProcess"]
