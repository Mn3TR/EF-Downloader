"""把「多个分卷」伪装成一个连续可寻址的字节流。

更新包是 54 个分卷，逻辑上等价于把 54 卷首尾相接得到的一个 53 GiB 单体
ZIP。标准库 ``zipfile`` 只接受一个 seekable 流，所以这里实现一个跨卷的
``RawIOBase``。

这样 **zip64 / deflate / CRC32 校验 / 中央目录解析全部由标准库负责**，
我们一行都不用写——这也是整个方案能这么短的原因。"""

from __future__ import annotations

import bisect
import io

from .volume import Volume

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
