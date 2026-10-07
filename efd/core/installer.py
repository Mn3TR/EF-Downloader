"""执行安装：流式解包，压缩包从不落盘。

峰值磁盘 = 最终体积 + 单文件缓冲。官方方式的峰值 = 压缩包 + 解压产物。

**安装循环只在这里实现一次**，CLI 与 GUI 共用；进度通过回调传出，
不依赖任何 UI 框架。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import journal
from .archive import Archive
from .planner import Plan
from .util import UnsafePathError, safe_join

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
        拷贝**途中**也会回调——只在文件边界回调的话，大文件写到一半时
        进度条和速度是冻住的，用户分不清「在干活」和「卡死了」。
    :param stop_event: 置位后在下一次读取前停机，最多再等一个 ``COPY_CHUNK``。
        半截文件会被删除——每个文件都是从头下载的，留残片没有意义。
    :param keep_going: 单个文件失败时继续，而不是中断。
    :param prune: 装完后删除 ``plan.stale``（本地存在但清单里没有的多余文件）。
    """
    target = plan.target
    os.makedirs(target, exist_ok=True)
    _sweep_parts(target, plan)

    result = Result()
    total = len(plan.need)
    total_u = plan.need_uncompressed
    total_c = plan.need_compressed
    started = last_t = time.monotonic()
    last_written = 0
    emitted_written = emitted_done = -1
    speed = 0.0
    # 这一轮真正算数的文件，装完记进安装日志——下次 `--prune` 才知道
    # 哪些是「本工具的」，可以回收；没记过的一律不碰。
    #
    # 已经装好的那些也要记：它们的路径和尺寸跟清单分毫不差，``_looks_installed``
    # 已经替我们认过一遍了。不记的话，日志启用之前装好的目录就永远是「外来文件」，
    # ``--prune`` 再也回收不了旧版本残留——而那正是这个功能存在的意义。
    placed: list[tuple[str, int]] = [(e.name, e.size) for e in plan.already]
    removed: list[str] = []

    def emit(current: str, force: bool = False, partial: int = 0) -> None:
        """播报进度。

        ``partial`` 是**当前文件已拷的字节数**——它还没算进 ``result.written``
        （那个只记已落盘完成的文件）。分开记是为了让「已完成」这个数字永远
        只包含真正 promote 成正式文件的字节，而进度条照样能往前走。
        """
        nonlocal last_t, last_written, speed, emitted_written, emitted_done
        now = time.monotonic()
        if not force and now - last_t < interval:
            return
        written = result.written + partial
        # 没有新进展就不重复播报——收尾那次 force 调用因此会在
        # "最后一个文件刚好报过进度" 时自然变成空操作。
        if written == emitted_written and result.done == emitted_done:
            return
        delta_t = max(now - last_t, 1e-9)
        # 速度只能是正的：拷贝途中报的是「已拷字节」，收尾那次按「已落盘字节」
        # 算，被中止的那半截一扣增量就成了负数——而停止之后停在界面上的
        # 恰好就是这一帧，用户看到的是「-9.18 GB/s」这种东西。
        speed = max(0.0, (written - last_written) / delta_t)
        last_t, last_written = now, written
        emitted_written, emitted_done = written, result.done
        if on_progress is None:
            return
        try:
            on_progress(
                Progress(
                    done=result.done,
                    total=total,
                    written=written,
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

    try:
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
            aborted = False
            try:
                with archive.open(entry) as src, open(tmp, "wb") as dst:
                    while True:
                        # 每块都查一次停机。只在文件之间查的话，「停止」要等整个
                        # 文件写完才生效，而最大的单文件有好几个 GB——用户按下去
                        # 之后界面十几分钟没反应，只会认为程序卡死了。
                        if stop_event is not None and stop_event.is_set():
                            aborted = True
                            break
                        buf = src.read(COPY_CHUNK)
                        if not buf:
                            break
                        dst.write(buf)
                        file_bytes += len(buf)
                        emit(entry.name, partial=file_bytes)
                if aborted:
                    # 半截文件没有任何价值：下次仍然从头下。绝不 promote 成正式文件。
                    _quiet_remove(tmp)
                else:
                    # 到这里 zipfile 已在 EOF 处校验过 CRC32；再原子替换。
                    os.replace(tmp, dest)
                    placed.append((entry.name, file_bytes))
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

            if aborted:
                result.stopped = True
                break

            result.written += file_bytes
            result.done += 1
            emit(entry.name)

        if prune and plan.stale and not result.stopped:
            removed = _prune(target, plan.stale)
            result.pruned = len(removed)
    finally:
        # 装完、中断、Ctrl-C——都得记。日志是 ``--prune`` 唯一的判据，
        # 少记一条只是少回收一个文件；漏记的绝不会被当成垃圾删掉。
        journal.update(target, added=placed, removed=removed)

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


def _sweep_parts(target: str, plan: Plan) -> None:
    """清掉上次被强杀留下的 ``.part``。

    关窗口时 worker 是 daemon 线程，进程会立刻退出，正在写的那个文件就留在
    盘上了。我们从不续写半截文件（每个文件都从头下），所以这些残片只有占地方
    一个作用——最大的单文件有 2 GB 往上。

    只碰「清单里那条路径 + ``.part``」，不会误删别的东西。
    """
    for entry in list(plan.already) + list(plan.need):
        try:
            tmp = safe_join(target, entry.name) + ".part"
        except UnsafePathError:
            continue
        try:
            os.remove(tmp)
        except OSError:
            pass


def _prune(target: str, names: list[str]) -> list[str]:
    """删除清单里已不存在的本地文件。只删文件，不删目录。

    返回**真的删掉了**的那些名字，好让调用方把记录一并更新掉——否则日志会
    一直记着已经不存在的文件，下次 ``--prune`` 又要白跑一遍。
    """
    removed: list[str] = []
    for name in names:
        try:
            os.remove(safe_join(target, name))
            removed.append(name)
        except (OSError, ValueError):
            pass
    return removed


__all__ = ["COPY_CHUNK", "Progress", "Result", "install"]
