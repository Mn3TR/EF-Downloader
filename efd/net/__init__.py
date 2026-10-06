"""网络与存储层：把 54 个远程分卷变成 zipfile 能读的连续字节流。

分成四块，各自只解决一件事：

* :mod:`efd.net.volume`   —— 分卷协议 + 本地/缺失实现
* :mod:`efd.net.http`     —— HTTP Range 分卷（块缓存、预读落点、重试）
* :mod:`efd.net.prefetch` —— 顺序预读线程池
* :mod:`efd.net.stream`   —— 跨卷拼接成 seekable 流
"""

from __future__ import annotations

from .http import RANGE_LOG_CAP, RangeError
from .http import HttpVolume
from .prefetch import Prefetcher
from .stream import ConcatReader, open_stream
from .volume import FileVolume, MissingVolume, Volume

__all__ = [
    "RANGE_LOG_CAP",
    "RangeError",
    "Prefetcher",
    "Volume",
    "FileVolume",
    "MissingVolume",
    "HttpVolume",
    "ConcatReader",
    "open_stream",
]
