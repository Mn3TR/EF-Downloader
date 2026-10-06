"""把「多个分卷」伪装成一个连续可寻址的字节流。

更新包是 54 个分卷，逻辑上等价于把 54 卷首尾相接得到的一个 53 GiB 单体 ZIP。
标准库 ``zipfile`` 只接受一个 seekable 流，所以这里实现一个跨卷的 ``RawIOBase``。

这样 **zip64 / deflate / CRC32 校验 / 中央目录解析全部由标准库负责**，
我们一行都不用写——这也是整个方案能这么短的原因。

三种 Volume 实现共用同一个 ``read_at(offset, n)`` 协议：

* :class:`HttpVolume`  —— 生产用，HTTP Range 随机读，带块缓存与流量计数
* :class:`FileVolume`  —— 本地已下载的分卷
* :class:`MissingVolume` —— 离线夹具用占位（保证偏移量正确，被读到就报错）
"""

from __future__ import annotations

import bisect
import io
import os
import urllib.request
from collections import OrderedDict
from typing import Protocol, runtime_checkable

from .config import DEFAULT_TIMEOUT, USER_AGENT

RANGE_LOG_CAP = 200


class RangeError(RuntimeError):
    """Range 请求没被正确满足。"""


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


class HttpVolume:
    """一卷 zip，按 HTTP Range 随机读取。

    两个必须保留的细节：

    * **块缓存**：中央目录的解析会在末尾反复小幅回看，缓存能把请求数压下来。
    * **206 校验**：若 CDN 不再支持 Range 而返回 200 全文件，偏移量会全错。
      静默接受这种响应会写出损坏的文件，所以宁可立刻报错。
    """

    def __init__(
        self,
        index: int,
        url: str,
        size: int,
        *,
        block: int = 1 << 20,
        cache_blocks: int = 8,
        timeout: int = DEFAULT_TIMEOUT,
        range_log: list | None = None,
    ):
        self.index = index
        self.url = url
        self.size = size
        self.block = block
        self.cache_blocks = cache_blocks
        self.timeout = timeout
        self.range_log = range_log
        # 统计：仅计真实下载量，命中缓存不计。
        self.bytes = 0
        self.requests = 0
        self._cache: OrderedDict[int, bytes] = OrderedDict()

    # -- 内部 ----------------------------------------------------------------
    def _fetch(self, offset: int, n: int) -> bytes:
        end = min(offset + n, self.size) - 1
        if end < offset:
            return b""
        req = urllib.request.Request(
            self.url,
            headers={"User-Agent": USER_AGENT, "Range": f"bytes={offset}-{end}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            status = getattr(resp, "status", None)
            data = resp.read()

        whole_file_ok = offset == 0 and n >= self.size
        if status not in (None, 206) and not whole_file_ok:
            raise RangeError(
                f"卷 {self.index:03d} 对 Range bytes={offset}-{end} 返回了 {status}"
                f"（期望 206）。CDN 可能已不支持范围请求。"
            )
        if len(data) > n and not whole_file_ok:
            raise RangeError(
                f"卷 {self.index:03d} 期望 {n} 字节却收到 {len(data)} 字节"
            )

        self.bytes += len(data)
        self.requests += 1
        if self.range_log is not None and len(self.range_log) < RANGE_LOG_CAP:
            self.range_log.append((self.index, offset, len(data)))
        return data

    def _block(self, index: int) -> bytes:
        start = index * self.block
        if start >= self.size:
            return b""
        want = min(self.block, self.size - start)
        blk = self._cache.get(index)
        if blk is None or len(blk) < want:
            blk = self._fetch(start, want)
            self._cache[index] = blk
        self._cache.move_to_end(index)
        while len(self._cache) > self.cache_blocks:
            self._cache.popitem(last=False)
        return blk

    # -- Volume 协议 ---------------------------------------------------------
    def read_at(self, offset: int, n: int) -> bytes:
        if n <= 0 or offset >= self.size:
            return b""
        offset = max(0, offset)
        n = min(n, self.size - offset)
        if n <= 0:
            return b""

        first = offset // self.block
        if self.cache_blocks > 0 and first == (offset + n - 1) // self.block:
            out = self._block(first)[offset - first * self.block :][:n]
            if len(out) == n:
                return out
        return self._fetch(offset, n)

    def close(self) -> None:
        self._cache.clear()

    def __repr__(self) -> str:
        return f"HttpVolume(index={self.index}, size={self.size}, bytes={self.bytes})"


class ConcatReader(io.RawIOBase):
    """把若干分卷拼成一个可 seek 的连续字节流。"""

    def __init__(self, volumes):
        self.volumes = list(volumes)
        self.starts: list[int] = []
        off = 0
        for vol in self.volumes:
            self.starts.append(off)
            off += vol.size
        self.total = off
        self.pos = 0

    # -- io 接口 -------------------------------------------------------------
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self.pos + offset
        elif whence == io.SEEK_END:
            pos = self.total + offset
        else:
            raise ValueError(f"无效的 whence: {whence}")
        self.pos = max(0, min(pos, self.total))
        return self.pos

    def read(self, n: int = -1) -> bytes:
        out = bytearray()
        if n is None or n < 0:
            n = self.total - self.pos
        n = min(n, self.total - self.pos)
        if n <= 0:
            return b""

        off = self.pos
        while n > 0:
            i = bisect.bisect_right(self.starts, off) - 1
            vol = self.volumes[i]
            inner = off - self.starts[i]
            take = min(n, vol.size - inner)
            chunk = vol.read_at(inner, take)
            if not chunk:
                raise RuntimeError(f"分卷 {i} 在偏移 {inner} 处提前 EOF")
            out += chunk
            off += len(chunk)
            n -= len(chunk)
        self.pos = off
        return bytes(out)

    def readinto(self, b) -> int:
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)

    def close(self) -> None:
        if self.closed:
            return
        for vol in self.volumes:
            try:
                vol.close()
            except Exception:  # noqa: BLE001 - 关闭失败不该掩盖真正的错误
                pass
        super().close()

    def __repr__(self) -> str:
        return f"ConcatReader({len(self.volumes)} 卷, total={self.total})"


def open_stream(volumes, buffer_size: int = 1 << 20) -> io.BufferedReader:
    """把分卷列表包成一个可直接喂给 ``zipfile`` 的缓冲流。"""
    return io.BufferedReader(ConcatReader(volumes), buffer_size=buffer_size)


__all__ = [
    "RANGE_LOG_CAP",
    "RangeError",
    "Volume",
    "FileVolume",
    "MissingVolume",
    "HttpVolume",
    "ConcatReader",
    "open_stream",
]
