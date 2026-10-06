"""规划：把「归档里有什么」和「本地已有什么」做差，得到该做什么。

**resume / 续传逻辑只在这里实现一次**，CLI 与 GUI 共用。
判定标准是「存在且尺寸相同就跳过」——不产生任何网络请求，所以续传是零成本的。
只有显式 ``verify_crc`` 时才会读盘校验内容。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .archive import Archive, Entry
from .util import UnsafePathError, crc32_file, safe_join


@dataclass
class Plan:
    """一次安装的完整计划。可序列化为 JSON。"""

    version: str
    target: str
    archive_bytes: int
    manifest_bytes: int
    entries_total: int
    already: list[Entry] = field(default_factory=list)
    need: list[Entry] = field(default_factory=list)
    excluded: list[Entry] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    unsafe: list[str] = field(default_factory=list)
    verified: bool = False

    # -- 汇总 ----------------------------------------------------------------
    @property
    def already_count(self) -> int:
        return len(self.already)

    @property
    def already_bytes(self) -> int:
        return sum(e.size for e in self.already)

    @property
    def need_count(self) -> int:
        return len(self.need)

    @property
    def need_uncompressed(self) -> int:
        return sum(e.size for e in self.need)

    @property
    def need_compressed(self) -> int:
        return sum(e.compressed for e in self.need)

    @property
    def biggest(self) -> int:
        """最大单文件——决定内存缓冲与续传粒度，也是峰值公式里的那一项。"""
        return max((e.size for e in self.need), default=0)

    @property
    def final_bytes(self) -> int:
        """装完之后目标目录应有的体积。"""
        return self.already_bytes + self.need_uncompressed

    @property
    def peak_ours(self) -> int:
        """本方案峰值 = 最终体积 + 单文件缓冲。压缩包从不落盘。"""
        return self.final_bytes + self.biggest

    @property
    def peak_official(self) -> int:
        """官方方式峰值 = 压缩包 + 解压产物，两者同时存在。"""
        return self.archive_bytes + self.final_bytes

    @property
    def saving(self) -> int:
        return self.peak_official - self.peak_ours

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "target": self.target,
            "archive_bytes": self.archive_bytes,
            "manifest_bytes": self.manifest_bytes,
            "entries_total": self.entries_total,
            "verified": self.verified,
            "already_count": self.already_count,
            "already_bytes": self.already_bytes,
            "need_count": self.need_count,
            "need_uncompressed": self.need_uncompressed,
            "need_compressed": self.need_compressed,
            "excluded_count": len(self.excluded),
            "biggest": self.biggest,
            "final_bytes": self.final_bytes,
            "peak_ours": self.peak_ours,
            "peak_official": self.peak_official,
            "saving": self.saving,
            "stale": self.stale,
            "unsafe": self.unsafe,
            # 全量列出，不截断——截断过的「计划」会骗人。
            "need": [e.to_dict() for e in self.need],
        }


def scan_local(root: str) -> dict[str, int]:
    """遍历目录，返回 ``相对路径(正斜杠) -> 尺寸``。只读。"""
    found: dict[str, int] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            try:
                found[rel] = os.stat(path).st_size
            except OSError:
                pass
    return found


def _excluded(name: str, prefixes) -> bool:
    return any(name.startswith(p) for p in prefixes)


def make_plan(
    archive: Archive,
    target: str,
    *,
    exclude_prefixes=(),
    verify_crc: bool = False,
    force: bool = False,
    limit: int = 0,
    detect_stale: bool = False,
) -> Plan:
    """生成计划。**只读**：不创建目录、不写文件。"""
    files = archive.files()
    excluded: list[Entry] = []
    candidates: list[Entry] = []
    for entry in files:
        (excluded if _excluded(entry.name, exclude_prefixes) else candidates).append(entry)

    already: list[Entry] = []
    need: list[Entry] = []
    unsafe: list[str] = []

    for entry in candidates:
        try:
            dest = safe_join(target, entry.name)
        except UnsafePathError:
            # 归档条目名不可信：既不装也不静默改写，单独列出来让人看见。
            unsafe.append(entry.name)
            continue
        if not force and _looks_installed(dest, entry, verify_crc):
            already.append(entry)
        else:
            need.append(entry)

    need.sort(key=lambda e: e.offset)  # 顺序扫描，零回退读
    if limit:
        need = need[:limit]

    stale: list[str] = []
    if detect_stale:
        manifest = {e.name for e in files}
        stale = sorted(
            name for name, size in scan_local(target).items()
            if size > 0 and name not in manifest
        )

    return Plan(
        version=archive.version,
        target=target,
        archive_bytes=archive.total_bytes,
        manifest_bytes=archive.net_bytes,
        entries_total=len(files),
        already=already,
        need=need,
        excluded=excluded,
        stale=stale,
        unsafe=unsafe,
        verified=verify_crc,
    )


def _looks_installed(dest: str, entry: Entry, verify_crc: bool) -> bool:
    """已存在且尺寸相同即视为装好；``verify_crc`` 时再算一次内容校验。"""
    try:
        if not os.path.exists(dest) or os.path.getsize(dest) != entry.size:
            return False
    except OSError:
        return False
    if not verify_crc:
        return True
    try:
        return crc32_file(dest) == entry.crc
    except OSError:
        return False


__all__ = ["Plan", "scan_local", "make_plan"]
