"""分卷协议与两种实现。

三种实现共用同一个 ``read_at(offset, n)`` 协议：

* :class:`HttpVolume`  —— 生产用，在 efd.net.http 里（HTTP Range 随机读）
* :class:`FileVolume`  —— 本地已下载的分卷
* :class:`MissingVolume` —— 离线夹具用占位（偏移量正确，被读到就报错）

``MissingVolume`` 保留真实 ``size`` 是关键：只有尺寸对，后面所有分卷的
起始偏移量才正确，才能在没有全部数据的情况下解析出中央目录。"""

from __future__ import annotations

import os

from typing import Protocol, runtime_checkable

@runtime_checkable
class Volume(Protocol):
    """分卷协议：只需要一个 ``size`` 和一个 ``read_at``。"""

    size: int

    def read_at(self, offset: int, n: int) -> bytes:  # pragma: no cover - 协议
        ...


class FileVolume:
    """本地已下载的分卷。"""

    def __init__(self, path: str):
        self.path = path
        self.size = os.path.getsize(path)
        self._f = open(path, "rb")

    def read_at(self, offset: int, n: int) -> bytes:
        self._f.seek(offset)
        return self._f.read(n)

    def close(self) -> None:
        self._f.close()

    def __repr__(self) -> str:
        return f"FileVolume({os.path.basename(self.path)!r}, size={self.size})"


class MissingVolume:
    """未下载的分卷占位。

    保留真实 ``size`` 是关键：只有尺寸对，后面所有分卷的起始偏移量才正确，
    才能在没有全部数据的情况下解析出中央目录（见 tests/test_archive_fixture.py）。
    """

    def __init__(self, size: int, index: int):
        self.size = size
        self.index = index

    def read_at(self, offset: int, n: int) -> bytes:
        raise RuntimeError(f"分卷 {self.index:03d} 不在本地（请求 offset={offset} n={n}）")

    def close(self) -> None:
        pass

    def __repr__(self) -> str:
        return f"MissingVolume(index={self.index}, size={self.size})"
