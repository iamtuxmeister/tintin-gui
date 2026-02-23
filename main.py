#!/usr/bin/env python3
"""
TinTin++ GUI — entry point.

Usage:
    python main.py                    # launch with no script
    python main.py myscript.tin       # load a script at startup
"""

import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore    import Qt

# Ensure our package root is on the path when running directly
import os
sys.path.insert(0, os.path.dirname(__file__))

from ui.main_window import MainWindow


def main():
    # HiDPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("TinTin++ GUI")
    app.setOrganizationName("tintin-gui")

    win = MainWindow()

    # Optional: load script from CLI
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(400, lambda: win._load_script_path(sys.argv[1]))

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
