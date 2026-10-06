"""执行安装：流式解包，压缩包从不落盘。

峰值磁盘 = 最终体积 + 单文件缓冲。官方方式的峰值 = 压缩包 + 解压产物。

**安装循环只在这里实现一次**，CLI 与 GUI 共用；进度通过回调传出，
不依赖任何 UI 框架。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

from .archive import Archive
from .planner import Plan
from .util import safe_join

COPY_CHUNK = 1 << 20


@dataclass
class Progress:
    done: int
    total: int
    written: int
    total_uncompressed: int
    net_bytes: int
    total_compressed: int
    speed: float  # B/s
    eta: float  # 秒
    current: str

    @property
    def fraction(self) -> float:
        """优先用「已下载的压缩字节」估算，比按文件数更接近真实进度。"""
        if self.total_compressed > 0 and self.net_bytes > 0:
            return min(self.net_bytes / self.total_compressed, 1.0)
        if self.total_uncompressed > 0:
            return min(self.written / self.total_uncompressed, 1.0)
        return 0.0


@dataclass
class Result:
    done: int = 0
    written: int = 0
    net_bytes: int = 0
    requests: int = 0
    elapsed: float = 0.0
    stopped: bool = False
    pruned: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.stopped


ProgressCb = Callable[[Progress], None]


def install(
    archive: Archive,
    plan: Plan,
    *,
    on_progress: ProgressCb | None = None,
    stop_event=None,
    interval: float = 0.5,
    keep_going: bool = False,
    prune: bool = False,
) -> Result:
    """按计划安装。

    :param on_progress: 进度回调，约每 ``interval`` 秒一次，外加最后一次。
    :param stop_event: 置位后在本文件写完时停机（不会留下半个文件）。
    :param keep_going: 单个文件失败时继续，而不是中断。
    :param prune: 装完后删除 ``plan.stale``（本地存在但清单里没有的多余文件）。
    """
    target = plan.target
    os.makedirs(target, exist_ok=True)

    result = Result()
    total = len(plan.need)
    total_u = plan.need_uncompressed
    total_c = plan.need_compressed
    started = last_t = time.monotonic()
    last_written = 0
    speed = 0.0

    def emit(current: str, force: bool = False) -> None:
        nonlocal last_t, last_written, speed
        now = time.monotonic()
        if not force and now - last_t < interval:
            return
        delta_t = max(now - last_t, 1e-9)
        speed = (result.written - last_written) / delta_t
        last_t, last_written = now, result.written
        if on_progress is None:
            return
        try:
            on_progress(
                Progress(
                    done=result.done,
                    total=total,
                    written=result.written,
                    total_uncompressed=total_u,
                    net_bytes=archive.net_bytes,
                    total_compressed=total_c,
                    speed=speed,
                    eta=(total_c - archive.net_bytes) / speed if speed > 0 else 0.0,
                    current=current,
                )
            )
        except Exception:  # noqa: BLE001 - 回调出错不该毁掉安装
            pass

    for entry in plan.need:
        if stop_event is not None and stop_event.is_set():
            result.stopped = True
            break

        dest = safe_join(target, entry.name)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)

        tmp = dest + ".part"
        file_bytes = 0
        try:
            with archive.open(entry) as src, open(tmp, "wb") as dst:
                while True:
                    buf = src.read(COPY_CHUNK)
                    if not buf:
                        break
                    dst.write(buf)
                    file_bytes += len(buf)
            # 到这里 zipfile 已在 EOF 处校验过 CRC32；再原子替换。
            os.replace(tmp, dest)
        except Exception as exc:  # noqa: BLE001
            _quiet_remove(tmp)
            message = f"{entry.name}: {exc}"
            result.errors.append(message)
            emit(f"失败 {entry.name}", force=True)
            if not keep_going:
                result.elapsed = time.monotonic() - started
                result.net_bytes = archive.net_bytes
                result.requests = archive.net_requests
                return result
            continue

        result.written += file_bytes
        result.done += 1
        emit(entry.name)

    if prune and plan.stale and not result.stopped:
        result.pruned = _prune(target, plan.stale)

    result.elapsed = time.monotonic() - started
    result.net_bytes = archive.net_bytes
    result.requests = archive.net_requests
    emit("", force=True)
    return result


def _quiet_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _prune(target: str, names: list[str]) -> int:
    """删除清单里已不存在的本地文件。只删文件，不删目录。"""
    removed = 0
    for name in names:
        try:
            os.remove(safe_join(target, name))
            removed += 1
        except (OSError, ValueError):
            pass
    return removed


__all__ = ["COPY_CHUNK", "Progress", "Result", "install"]
