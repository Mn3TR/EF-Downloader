"""记住用户上次选过的安装目录、并发数与限速。

自动探测（:mod:`efd.core.detect`）通常够用，但它失败的时候用户得手动挑一次
目录——下次启动却又忘光，这就很难受。所以把选择落盘。

全部是**尽力而为**：读写失败一律静默忽略，绝不能因为一个偏好设置文件
让界面起不来。

磁盘格式是一个扁平 JSON 对象，只认三个字符串键：

    {"target": "D:\\...\\Arknights Endfield", "jobs": "8", "rate": "0"}

**类型不对或全是空白就退回到默认值**——用户手改过这个文件、或者旧版本写过
别的形状，都不该让界面拿到一个 `None` 再去崩在别处。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from . import detect
from .throttle import DEFAULT_JOBS

FILE_NAME = "settings.json"

# 网络选项的出厂默认值。并发直接取 throttle.DEFAULT_JOBS（实测出来的拐点，见
# 那里的注释），不在这里另立一份——两处默认值迟早会走岔。限速默认关。
DEFAULT_RATE = "0"
DEFAULT_JOBS_TEXT = str(DEFAULT_JOBS)


def config_path() -> Path:
    """偏好设置文件位置。Windows 下放 ``%LOCALAPPDATA%\\EFD``。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    return root / "EFD" / FILE_NAME


def _text(value: object, fallback: str = "") -> str:
    """只接受非空白的字符串，其余一律退回 ``fallback``。

    这一层存在的意义：设置文件是**用户能直接编辑**的，任何类型的垃圾都可能
    出现在里面（``{"target": 12345}``、``null``、``""``）。所有校验集中在这
    一个函数里，调用方拿到的永远是干净字符串，不必各自再判一次。
    """
    return value if isinstance(value, str) and value.strip() else fallback


@dataclass(frozen=True)
class Settings:
    """落盘的偏好。字段都有出厂默认值，缺一个键不影响另外两个。"""

    target: str = ""
    jobs: str = DEFAULT_JOBS_TEXT
    rate: str = DEFAULT_RATE

    @classmethod
    def from_mapping(cls, raw: object) -> Settings:
        """从 JSON 对象构造。任何畸形输入都退化成默认值，绝不抛异常。"""
        if not isinstance(raw, dict):
            return cls()
        return cls(
            target=_text(raw.get("target")),
            jobs=_text(raw.get("jobs"), DEFAULT_JOBS_TEXT),
            rate=_text(raw.get("rate"), DEFAULT_RATE),
        )

    def to_mapping(self) -> dict[str, str]:
        return {"target": self.target, "jobs": self.jobs, "rate": self.rate}


def load() -> Settings:
    try:
        with open(config_path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return Settings()
    return Settings.from_mapping(raw)


def save(settings: Settings) -> None:
    try:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(settings.to_mapping(), fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _update(**changes: str) -> None:
    """读—改—写。变化为空时不落盘，省掉无谓的磁盘写。"""
    current = load()
    updated = replace(current, **changes)
    if updated != current:
        save(updated)


def remembered_target() -> str:
    return load().target


def remember_target(target: str) -> None:
    if not target.strip():
        return
    _update(target=target)


def initial_target() -> str:
    """界面启动时的默认目录：上次用过的优先，其次自动探测，都没有就留空。"""
    return remembered_target() or detect.suggest_target()


def remembered_net() -> tuple[str, str]:
    """上次用的 (并发数, 限速)，都按字符串返回，直接喂给界面控件。"""
    settings = load()
    return settings.jobs, settings.rate


def remember_net(jobs: str, rate: str) -> None:
    """记住网络选项。空白的输入退回出厂值，而不是把「空」记成一个设置。"""
    _update(jobs=_text(jobs, DEFAULT_JOBS_TEXT), rate=_text(rate, DEFAULT_RATE))


__all__ = [
    "DEFAULT_JOBS_TEXT",
    "DEFAULT_RATE",
    "Settings",
    "config_path",
    "initial_target",
    "load",
    "remember_net",
    "remember_target",
    "remembered_net",
    "remembered_target",
    "save",
]
