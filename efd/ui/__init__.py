"""tkinter 界面层。

只依赖 ``efd.core`` 与 ``efd.net``；反过来它们都不知道界面的存在，
所以这一层可以整个换掉而不碰任何逻辑。
"""

from __future__ import annotations

from .app import App
from .messages import TERMINAL_KINDS, Msg, Worker
from .theme import APP_TITLE, TAGLINE

__all__ = [
    "APP_TITLE",
    "TAGLINE",
    "App",
    "Msg",
    "Worker",
    "TERMINAL_KINDS",
]
