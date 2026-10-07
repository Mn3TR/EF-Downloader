"""规划：把「归档里有什么」和「本地已有什么」做差，得到该做什么。

**resume / 续传逻辑只在这里实现一次**，CLI 与 GUI 共用。
判定标准是「存在且尺寸相同就跳过」——不产生任何网络请求，所以续传是零成本的。
只有显式 ``verify_crc`` 时才会读盘校验内容。

本模块**只读**：``make_plan`` 不创建目录、不写文件，连安装日志也只读不写。
真正动盘的是 ``installer``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import journal
from .archive import Archive, Entry
from .config import HOT_UPDATE_PATHS
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
    foreign: list[str] = field(default_factory=list)
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
            "foreign": self.foreign,
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


def _split_leftovers(
    target: str, manifest: set[str], logged: dict[str, int]
) -> tuple[list[str], list[str]]:
    """把「本地有、清单没有」的文件分成 ``(可以删的, 只能报告的)``。

    分界线是**安装日志**：``logged`` 是 ``journal.load(target)``，记着本工具
    当初确实往这个目录写过哪些文件、各写了多少字节。

    判据是「日志记过 **且尺寸分毫不差**」才算残留——文件被谁改过就说明它已经
    易主，不再归本工具处置。这换来了三个想要的性质：

    * 游戏运行期写的存档 / 日志 / 缓存（从没被记过）**永远不会被删**；
    * 旧版本删剩的残留（当初是本工具写的）照常回收；
    * 整目录被删掉的旧版本残留，即使目录结构已经和运行期产物长得一样，
      也能靠「名字在日志里」认出来——这正是两条结构判据栽跟头的地方。

    没用日志记过的一律进第二个桶：只报告，不删。所以**日志启用前就装好的
    老目录，第一次跑不会删任何东西**，先跑一次 ``install`` 建立记录即可。

    两个桶都排除 ``HOT_UPDATE_PATHS``：通道 B 的地盘不归 ``--prune`` 管。
    """
    stale: list[str] = []
    foreign: list[str] = []
    for name, size in scan_local(target).items():
        if size <= 0 or name in manifest:
            continue
        # 通道 B 会往这些子树里新增文件，它们不在这份清单里，但**不是多余**。
        # 少了这一条，`--prune` 会把热更新下来的资源当垃圾删掉。
        if _excluded(name, HOT_UPDATE_PATHS):
            continue
        if logged.get(name) == size:
            stale.append(name)
        else:
            foreign.append(name)
    return sorted(stale), sorted(foreign)


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
    foreign: list[str] = []
    if detect_stale:
        manifest = {e.name for e in files}
        stale, foreign = _split_leftovers(target, manifest, journal.load(target))

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
        foreign=foreign,
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
