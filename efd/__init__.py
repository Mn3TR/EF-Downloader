"""EFD —— 《明日方舟：终末地》省空间下载器。

把官方 54 分卷更新包当作一个连续 ZIP 流式读取，边下边解，
**从不把压缩包落盘**，因此峰值磁盘只需「最终体积 + 单文件缓冲」。

设计要点见 docs/REPORT.md。
"""

from __future__ import annotations

__version__ = "0.2.3"

__all__ = ["__version__"]
