"""界面外观：标题、尺寸、配色与 ttk 样式。

集中在这里是为了让「长什么样」和「做什么」分开：面板代码只管摆放控件，
换皮肤不用碰任何逻辑。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

APP_TITLE = "EF DOWNLOADER"
TAGLINE = "边下边解压 · 峰值磁盘占用约为官方方式的一半"

POLL_MS = 120
WINDOW = (980, 760)
MIN_WINDOW = (880, 640)

# 刻意**不再写死**成开发者自己的路径——那对别人毫无意义，只会让人怀疑
# 「这不是给我的东西」。默认值由 efd.core.settings 决定：上次用过的 → 注册表探测 → 留空。
DEFAULT_TARGET = ""

MUTED = "#666666"
GREEN = "#1a7f37"
ORANGE = "#b45309"
RED = "#b91c1c"


def apply_styles(root: tk.Misc) -> None:
    """装好 ttk 主题与这套界面用到的全部具名样式。"""
    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass
    root.option_add("*Font", ("Microsoft YaHei UI", 10))
    style.configure("Head.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
    style.configure("Tag.TLabel", foreground=MUTED)
    style.configure("Name.TLabel", foreground=MUTED)
    style.configure("Value.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
    style.configure("Save.TLabel", foreground=GREEN,
                    font=("Microsoft YaHei UI", 11, "bold"))
    style.configure("Pct.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
    style.configure("Stat.TLabel", font=("Consolas", 10))
    style.configure("Hint.TLabel", foreground=MUTED)
    style.configure("Action.TButton", padding=(16, 9))
