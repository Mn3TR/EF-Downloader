"""把 54 个分卷当作一个 ZIP 打开，并提供稳定的条目视图。

对上层暴露 :class:`Entry` 而不是 ``zipfile.ZipInfo``，一是让 planner/GUI 不必
了解 zipfile 细节，二是让计划可以直接序列化成 JSON。
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from dataclasses import dataclass

from . import seed as seed_mod
from .volumes import FileVolume, HttpVolume, MissingVolume, open_stream


class ArchiveError(RuntimeError):
    """归档无法打开或内容异常。"""


@dataclass(frozen=True)
class Entry:
    """归档中的一个条目。

    ``method`` 是压缩方法（``zipfile.ZIP_STORED`` / ``ZIP_DEFLATED``）。
    """

    name: str
    size: int  # 解压后
    compressed: int  # 压缩后
    crc: int
    offset: int  # 归档内本地头偏移，用于顺序扫描
    method: int
    is_dir: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "size": self.size,
            "compressed": self.compressed,
            "crc": self.crc,
            "offset": self.offset,
            "method": self.method,
        }


class Archive:
    """一个已打开的更新包归档。

    用 ``with`` 管理生命周期::

        with open_remote() as ar:
            for entry in ar.files():
                ...
    """

    def __init__(self, version: str, volumes: list, stream: io.BufferedReader,
                 zf: zipfile.ZipFile):
        self.version = version
        self.volumes = volumes
        self._stream = stream
        self.zf = zf
        self._by_name: dict[str, zipfile.ZipInfo] = {}
        self.entries: list[Entry] = []
        for info in zf.infolist():
            self._by_name.setdefault(info.filename, info)
            self.entries.append(
                Entry(
                    name=info.filename,
                    size=info.file_size,
                    compressed=info.compress_size,
                    crc=info.CRC,
                    offset=info.header_offset,
                    method=info.compress_type,
                    is_dir=info.is_dir(),
                )
            )

    # -- 基本属性 -------------------------------------------------------------
    @property
    def total_bytes(self) -> int:
        """归档的逻辑总长度（所有分卷之和）。"""
        return self._stream.raw.total

    @property
    def net_bytes(self) -> int:
        """真实下载字节数。本地卷恒为 0。"""
        return sum(getattr(v, "bytes", 0) for v in self.volumes)

    @property
    def net_requests(self) -> int:
        return sum(getattr(v, "requests", 0) for v in self.volumes)

    # -- 条目视图 -------------------------------------------------------------
    def files(self) -> list[Entry]:
        """非目录条目。"""
        return [e for e in self.entries if not e.is_dir]

    def dirs(self) -> list[Entry]:
        return [e for e in self.entries if e.is_dir]

    def open(self, entry: Entry):
        """打开条目为只读流。

        注意用流而不是 ``zf.read()``：最大单文件 1.50 GB，
        一次性读入会让内存峰值等于文件大小。CRC32 仍由 zipfile 在读到 EOF 时校验。
        """
        info = self._by_name.get(entry.name)
        if info is None:
            raise ArchiveError(f"归档中没有条目: {entry.name}")
        return self.zf.open(info)

    def read(self, entry: Entry) -> bytes:
        with self.open(entry) as f:
            return f.read()

    # -- 生命周期 -------------------------------------------------------------
    def close(self) -> None:
        try:
            self.zf.close()
        finally:
            try:
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return (f"Archive(version={self.version!r}, 条目={len(self.entries)}, "
                f"total={self.total_bytes})")


# ---------------------------------------------------------------- 打开方式


def open_remote(*, block: int = 1 << 20, release: seed_mod.Release | None = None,
                range_log: list | None = None) -> Archive:
    """联网打开最新全量包。"""
    rel = release or seed_mod.fetch_release()
    volumes: list = [
        HttpVolume(p.index, p.url, p.size, range_log=range_log) for p in rel.packs
    ]
    # 中央目录在末卷，1 MiB 块足以让反复回看命中缓存。
    stream = open_stream(volumes, buffer_size=block)
    try:
        zf = zipfile.ZipFile(stream)
    except Exception:
        stream.close()
        raise
    return Archive(rel.version, volumes, stream, zf)


def open_local(directory: str, sizes: list[int], *, version: str = "(local)",
               block: int = 1 << 20) -> Archive:
    """离线打开。

    ``directory`` 里存在 ``volNNN.bin`` 的用真实文件，其余按 ``sizes`` 占位。
    只要**尺寸对**，即使只有末卷也能解析出中央目录——这是离线夹具能工作的原理。
    """
    if not sizes:
        raise ArchiveError("sizes 为空，无法确定分卷布局")

    volumes: list = []
    for i, size in enumerate(sizes, start=1):
        path = os.path.join(directory, f"vol{i:03d}.bin")
        volumes.append(FileVolume(path) if os.path.exists(path) else MissingVolume(size, i))

    stream = open_stream(volumes, buffer_size=block)
    try:
        zf = zipfile.ZipFile(stream)
    except Exception:
        stream.close()
        raise
    return Archive(version, volumes, stream, zf)


def load_pack_sizes(path: str) -> list[int]:
    """读 ``pack_sizes.json`` 夹具。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    sizes = data["sizes"] if isinstance(data, dict) else data
    return [int(s) for s in sizes]


__all__ = [
    "ArchiveError",
    "Entry",
    "Archive",
    "open_remote",
    "open_local",
    "load_pack_sizes",
]
