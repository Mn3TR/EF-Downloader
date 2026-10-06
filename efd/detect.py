"""尽力找出游戏装在哪。

这个模块存在的唯一理由：**默认值不能写死成开发者自己的路径**。
别人拿到 exe 时，`D:\\Apps\\Hypergryph Launcher\\games\\Arknights Endfield`
这种值毫无意义，只会让人怀疑「这不是给我的东西」。

发现顺序：
  1. 注册表里鹰角启动器的安装位置 → 扫 ``<启动器>\\games\\*``
  2. 启动器目录本身
  3. 都找不到 → 返回空，让调用方去提示用户手选

识别标志是 ``Endfield.exe`` 或 ``Endfield_Data``，两者出现其一即认为是游戏目录。
纯函数（:func:`looks_like_game_dir` / :func:`scan_games_dir`）与注册表读取分开，
前者可以脱离 Windows 测试。
"""

from __future__ import annotations

import os
from pathlib import Path

GAME_MARKERS = ("Endfield.exe", "Endfield_Data")
DEFAULT_GAME_NAME = "Arknights Endfield"

# 启动器在注册表里的位置。
# 注意 hive 名字必须与 winreg 的常量**逐字一致**（HKEY_CURRENT_USER 而不是 HKCU）——
# 之前在这里凭印象写了缩写，getattr 拿到 None 又被静默跳过，
# 结果探测永远返回空、而测试全绿。现在写成真名，并且拿不到就报错。
REGISTRY_KEYS = (
    ("HKEY_CURRENT_USER", r"Software\Hypergryph\Launcher"),
    ("HKEY_LOCAL_MACHINE", r"Software\Hypergryph\Launcher"),
    ("HKEY_LOCAL_MACHINE", r"Software\WOW6432Node\Hypergryph\Launcher"),
)


def looks_like_game_dir(path: str | os.PathLike) -> bool:
    """目录里有 ``Endfield.exe`` 或 ``Endfield_Data`` 就认为是游戏目录。"""
    try:
        base = Path(path)
    except (TypeError, ValueError):
        return False
    return any((base / marker).exists() for marker in GAME_MARKERS)


def scan_games_dir(games_dir: str | os.PathLike) -> str | None:
    """在 ``<启动器>\\games\\`` 下找游戏目录。"""
    try:
        entries = sorted(Path(games_dir).iterdir())
    except OSError:
        return None
    for child in entries:
        if child.is_dir() and looks_like_game_dir(child):
            return str(child)
    return None


def launcher_paths() -> list[str]:
    """从注册表读启动器安装位置。

    非 Windows、注册表读不到、或键不存在时返回空列表——调用方不该因为
    读不到注册表就失败。
    """
    try:
        import winreg
    except ImportError:  # 非 Windows
        return []

    found: list[str] = []
    for hive_name, subkey in REGISTRY_KEYS:
        hive = getattr(winreg, hive_name, None)
        if hive is None:
            # 常量名写错是**编码错误**，不是「这台机器没装启动器」。
            # 静默跳过会让探测永远返回空，而外面完全看不出问题。
            raise RuntimeError(
                f"winreg 里没有 {hive_name}；REGISTRY_KEYS 写错了"
            )
        try:
            with winreg.OpenKey(hive, subkey) as key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    child = winreg.EnumKey(key, index)
                    try:
                        with winreg.OpenKey(key, child) as sub:
                            value, _ = winreg.QueryValueEx(sub, "install_path")
                    except OSError:
                        continue
                    if isinstance(value, str) and value and value not in found:
                        found.append(value)
        except OSError:
            # 这个 hive / 子键不存在是正常的（比如按机器安装时 HKCU 里没有）
            continue
    return found


def find_game_dir() -> str | None:
    """返回已安装的游戏目录；找不到返回 ``None``。"""
    for launcher in launcher_paths():
        found = scan_games_dir(os.path.join(launcher, "games"))
        if found:
            return found
        if looks_like_game_dir(launcher):
            return launcher
    return None


def suggest_target() -> str:
    """给界面用的默认目标目录。

    * 已经装好了 → 就填它（续传/补齐场景）
    * 只有启动器 → 填启动器约定俗成的游戏路径（安装会自己建目录）
    * 什么都没有 → 空串，让用户自己选
    """
    found = find_game_dir()
    if found:
        return found
    for launcher in launcher_paths():
        return str(Path(launcher) / "games" / DEFAULT_GAME_NAME)
    return ""


__all__ = [
    "GAME_MARKERS",
    "DEFAULT_GAME_NAME",
    "looks_like_game_dir",
    "scan_games_dir",
    "launcher_paths",
    "find_game_dir",
    "suggest_target",
]
