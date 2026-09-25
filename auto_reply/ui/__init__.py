"""Windows 本机控制台（Tkinter）。启动：pythonw tools/run_dashboard.py [--demo]"""

from .app import Dashboard, main
from .backend import LiveBackend

__all__ = ["Dashboard", "LiveBackend", "main"]
