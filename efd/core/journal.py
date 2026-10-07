"""安装日志：记住「这个目录里哪些文件是本工具放进去的」。

为什么需要它：``--prune`` 要删的是「旧版本留下、新清单已不含」的残留，但
「本地有、清单没有」这个条件**不足以**识别残留——游戏运行期自己也会往目录里
写存档 / 日志 / 缓存，它们同样不在清单里，删掉会重置登录态。

两条更「聪明」的判据都已被实测证伪，记在这里免得后人再试一遍：

1. **结构判据**（父目录没被清单声明 ⇒ 运行期产物）：``mmkv/``（运行期建的）
   与 ``leftover/``（旧版本删剩的）结构完全相同，都是未声明的父目录，
   无从区分。用它的后果是真正该删的旧残留被静默放过。
2. **静态路径清单**（把见过的运行期路径写死）：兜不住。游戏每次运行都会写出
   事先不可枚举的新路径——实测第一次装完是 12 个，第二次启动后涨到 34 个，
   其中还包含 ``Endfield_Data/Persistent/`` 这种整个子树都是新的情况。

所以改成**记日志**：只有本工具确实写过的文件，才可能被 ``--prune`` 删掉；
没被记过的一律不碰。取舍依然是「**宁可漏删占地方，不可误删丢登录态**」。

代价是：日志启用之前就装好的目录，第一次跑什么都不会删（没有记录）。这是
预期行为，不是 bug——先跑一次 ``install`` 把清单里的文件记上，之后才谈得上回收。

日志放在 ``%LOCALAPPDATA%\\EFD\\journal.json``，不污染游戏目录。
和 ``settings`` 一样是**尽力而为**：读写失败一律静默忽略，最坏退化成「不删」。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from . import settings

FILE_NAME = "journal.json"

# 最多记住几个目标目录。装完就丢的临时目录不该把日志撑成无限增长的文件；
# 超出时淘汰最久没动过的那些（写回时最近更新的排在最后）。
MAX_TARGETS = 8


def state_path() -> Path:
    """安装日志位置。Windows 下是 ``%LOCALAPPDATA%\\EFD\\journal.json``。"""
    return settings.state_dir() / FILE_NAME


def _key(target: str) -> str:
    """把目标目录规范化成日志里的键。

    同一个目录可能被写成 ``D:\\Games\\EFD``、``d:/games/efd`` 或带 ``..`` 的
    形式；``normcase`` + ``abspath`` 之后它们是同一个键，不会各记一份。
    """
    return os.path.normcase(os.path.abspath(target)).replace("\\", "/")


def _read() -> dict[str, dict[str, int]]:
    """读出全部记录。任何畸形内容都当作不存在——日志坏掉只该让 prune 变保守。"""
    try:
        with open(state_path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    targets = raw.get("targets")
    if not isinstance(targets, dict):
        return {}

    out: dict[str, dict[str, int]] = {}
    for key, files in targets.items():
        if not isinstance(key, str) or not key or not isinstance(files, dict):
            continue
        clean: dict[str, int] = {}
        for name, size in files.items():
            # bool 是 int 的子类，得单独挡掉——否则 ``true`` 会被当成尺寸 1。
            if not isinstance(name, str) or not name:
                continue
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                continue
            clean[name] = size
        if clean:
            out[key] = clean
    return out


def _write(targets: dict[str, dict[str, int]]) -> None:
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        trimmed = dict(list(targets.items())[-MAX_TARGETS:])
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"targets": trimmed}, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def load(target: str) -> dict[str, int]:
    """取某个目标目录的记录：``相对路径(正斜杠) -> 当初写下去的字节数``。"""
    return dict(_read().get(_key(target), {}))


def update(
    target: str,
    *,
    added: Iterable[tuple[str, int]] = (),
    removed: Iterable[str] = (),
) -> None:
    """读—改—写。记下这次落盘的文件，忘掉刚被删掉的。

    ``removed`` 存在是为了让日志和磁盘一致：删掉的文件如果还留在日志里，
    下次它要是被别的程序重新创建出来，尺寸对不上（见下），不会误删——但
    留着一条死记录没有意义。

    尺寸是**故意**记的：``make_plan`` 只把「日志记过 **且尺寸分毫不差**」的
    文件当残留。文件被谁改过，就说明它已经易主，不再归本工具处置。
    """
    targets = _read()
    key = _key(target)
    current = targets.get(key, {})

    merged = dict(current)
    for name, size in added:
        if isinstance(name, str) and name:
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                continue
            merged[name] = size
    for name in removed:
        merged.pop(name, None)

    if merged == current:
        return
    # 位置决定淘汰顺序：重新插到最后，最久没动过的自然排到前面去。
    targets.pop(key, None)
    if merged:
        targets[key] = merged
    _write(targets)


__all__ = ["FILE_NAME", "MAX_TARGETS", "load", "state_path", "update"]
