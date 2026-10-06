"""记住用户上次选过的安装目录。

自动探测（:mod:`efd.detect`）通常够用，但它失败的时候用户得手动挑一次目录——
下次启动却又忘光，这就很难受。所以把选择落盘。

全部是**尽力而为**：读写失败一律静默忽略，绝不能因为一个偏好设置文件
让界面起不来。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

FILE_NAME = "settings.json"


def config_path() -> Path:
    """偏好设置文件位置。Windows 下放 ``%LOCALAPPDATA%\\EFD``。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    return root / "EFD" / FILE_NAME


def load() -> dict:
    try:
        with open(config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    try:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def remembered_target() -> str:
    value = load().get("target")
    return value if isinstance(value, str) else ""


def remember_target(target: str) -> None:
    if not target.strip():
        return
    data = load()
    if data.get("target") == target:
        return
    data["target"] = target
    save(data)


def initial_target() -> str:
    """界面启动时的默认目录：上次用过的优先，其次自动探测，都没有就留空。"""
    from . import detect  # 延迟导入，settings 被别处引用时不牵连注册表读取

    return remembered_target() or detect.suggest_target()


__all__ = [
    "config_path",
    "initial_target",
    "load",
    "remember_target",
    "remembered_target",
    "save",
]
