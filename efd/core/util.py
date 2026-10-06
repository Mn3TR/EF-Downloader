"""无状态小工具。零依赖，纯标准库。"""

from __future__ import annotations

import os
import shutil
import sys
import zlib

CHUNK = 1 << 20


def setup_output_encoding() -> None:
    """让中文输出在各种消费方那里都稳定。

    Windows 上 Python 在**管道/重定向**时用本地代码页（简中 = cp936），
    而现代消费方（PowerShell 7、多数 IDE、CI）按 UTF-8 读，于是整段变成
    ``U+FFFD`` 替换字符——实测 ``EFD.exe --help > out.txt`` 得到的文件里
    没有一个可读汉字。

    控制台场景**刻意不动**：那里 Python 跟随控制台代码页，本来就是对的
    （cp936 控制台得到 cp936，UTF-8 控制台得到 UTF-8）。

    CLI 与构建脚本都调用它，免得同一类问题各修一遍。
    """
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


class UnsafePathError(ValueError):
    """归档内的条目名试图逃出目标目录。"""


def human(n: float) -> str:
    """1536 -> '1.50 KB'。用于所有面向人的体积显示。"""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:,.2f} {unit}"
        n /= 1024
    return f"{n:,.2f} PB"


def human_time(seconds: float) -> str:
    """秒 -> 'H:MM:SS' 或 'MM:SS'；非法/未知返回 '--:--'。"""
    if seconds is None or seconds != seconds or seconds in (float("inf"), float("-inf")):
        return "--:--"
    if seconds <= 0:
        return "--:--"
    sec = int(seconds)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def crc32_file(path: str, chunk: int = CHUNK) -> int:
    """整文件 CRC32。读满全盘，慢，只在 ``--verify-crc`` 下用。"""
    crc = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            crc = zlib.crc32(block, crc)
    return crc & 0xFFFFFFFF


def safe_join(root: str, rel: str) -> str:
    """把归档条目名拼到目标目录下，越界则报错。

    与「静默丢弃 .. 分量」的做法不同，这里选择**显式拒绝**：条目名异常说明
    归档本身不可信，此时悄悄改写路径比直接报错危险得多。

    实测 1692 条清单里没有任何 ``..`` / 绝对路径 / 反斜杠，
    所以拒绝策略不会误伤正常包。
    """
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or (len(rel) >= 2 and rel[1] == ":"):
        raise UnsafePathError(f"绝对路径: {rel!r}")

    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise UnsafePathError(f"路径包含 '..': {rel!r}")

    root_abs = os.path.abspath(root)
    dest = os.path.abspath(os.path.join(root_abs, *parts))
    try:
        inside = os.path.commonpath([dest, root_abs]) == root_abs
    except ValueError:  # 不同盘符
        inside = False
    if not inside:
        raise UnsafePathError(f"路径越界: {rel!r}")
    return dest


def free_bytes(path: str) -> int:
    """path 所在盘的可用空间；path 不存在时向上找到最近的已存在祖先。

    返回 0 表示查询失败（调用方应视为「未知」，不要当成「满了」直接拒绝）。
    """
    probe = os.path.abspath(path)
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe or os.path.abspath(os.sep)).free
    except OSError:
        return 0


__all__ = [
    "CHUNK",
    "UnsafePathError",
    "setup_output_encoding",
    "human",
    "human_time",
    "crc32_file",
    "safe_join",
    "free_bytes",
]
