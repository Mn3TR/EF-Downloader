"""进程入口：摘控制台、开高 DPI 感知、起主窗口。"""

from __future__ import annotations

import ctypes
import sys

from .app import App


def detach_console() -> None:
    """冻结成 exe 后，把控制台窗口从 GUI 进程上摘掉。

    源码运行时什么都不做。这样**同一个 exe** 既能双击出图形界面，
    也能在终端里跑 ``efd plan`` 这类命令——不需要打包两个可执行文件。
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        ctypes.windll.kernel32.FreeConsole()
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    detach_console()
    try:  # 高 DPI 屏下不糊
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    App().mainloop()
