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
import concurrent.futures
import io
import os
import threading
import urllib.request
from collections import OrderedDict
from typing import Protocol, runtime_checkable

from .config import DEFAULT_TIMEOUT, USER_AGENT

RANGE_LOG_CAP = 200


class RangeError(RuntimeError):
    """Range 请求没被正确满足。"""


class Prefetcher:
    """顺序预读用的共享线程池。

    ``jobs`` 是**总**并发数：1 个消费者线程 + ``jobs - 1`` 个预读线程。
    ``jobs == 1`` 时根本不建池，也就是完全没有预读（改动前的行为）。

    预读只对**顺序前向读**有意义。安装计划本来就按偏移排好序
    （``planner.make_plan`` 里的 ``need.sort(key=lambda e: e.offset)``），
    所以消费者是单调前进的，下一个要读的区间可以精确预测：
    就是 ``offset + n``、``offset + 2n`` …… 依次类推。

    这也意味着预读**不会**退回散点随机读——那条路我们量过，8 并发也只有
    2.7 → 3.3 MB/s（见 docs/REPORT.md）。预读只是把顺序读里每次请求的
    建连与握手延迟叠起来，读的仍是同一条顺序路径。
    """

    def __init__(self, jobs: int = 1):
        self.jobs = max(1, int(jobs))
        self.fetched = 0  # 预读提前拿到、并且真的被用上的字节
        self.wasted = 0  # 预读拿了却没人要的字节（计划有空洞时的上界）
        self._stat_lock = threading.Lock()
        self._pool = (
            concurrent.futures.ThreadPoolExecutor(
                max_workers=self.jobs - 1, thread_name_prefix="efd-prefetch"
            )
            if self.jobs > 1
            else None
        )

    @property
    def enabled(self) -> bool:
        return self._pool is not None

    def note_used(self, n: int) -> None:
        with self._stat_lock:
            self.fetched += n

    def note_wasted(self, n: int) -> None:
        with self._stat_lock:
            self.wasted += n

    def submit(self, fn, *args):
        if self._pool is None:
            return None
        try:
            return self._pool.submit(fn, *args)
        except RuntimeError:  # 池已经关了（收尾阶段还在预读）
            return None

    def shutdown(self) -> None:
        """收尾。**故意不取消**排队中的任务。

        ``cancel_futures=True`` 会让还没开跑的任务永远不执行，于是它们登记
        的取块权再也没人交还——任何等在那个块上的线程都会挂死。让它们跑完
        更安全：任务一进来就看见 ``_closed``，立刻原样退出，代价只有一次判断。
        """
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False)

    def __repr__(self) -> str:
        return f"Prefetcher(jobs={self.jobs}, fetched={self.fetched}, wasted={self.wasted})"


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

    必须保留的细节：

    * **块缓存**：中央目录的解析会在末尾反复小幅回看，缓存能把请求数压下来。
      它同时也是顺序预读的落点——请求先对齐到块，块才能被提前取来复用。
    * **206 校验**：若 CDN 不再支持 Range 而返回 200 全文件，偏移量会全错。
      静默接受这种响应会写出损坏的文件，所以宁可立刻报错。
    * **锁只护住缓存和计数**：网络请求一律在锁外发，否则预读线程会互相排队，
      并发就等于没加。
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
        limiter=None,
        prefetcher: Prefetcher | None = None,
    ):
        self.index = index
        self.url = url
        self.size = size
        self.block = block
        self.timeout = timeout
        self.range_log = range_log
        self.limiter = limiter
        self.prefetcher = prefetcher
        # 预读会占住 cache_blocks 里的若干格；格子不够时预读的块刚取来就被淘汰，
        # 既浪费流量又白费线程。留出 jobs 个前向格再加两格回看余量。
        jobs = prefetcher.jobs if prefetcher is not None else 1
        self.cache_blocks = max(cache_blocks, jobs + 2) if cache_blocks > 0 else 0
        # 统计：仅计真实下载量，命中缓存不计。
        self.bytes = 0
        self.requests = 0
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._lock = threading.Lock()
        self._closed = False
        # 正在被预读线程取的块。消费者撞上「正在取」的同一块时要**等它**，
        # 绝不能自己再发一次请求，否则这块会被下载两遍，预读反而让流量翻倍。
        # 值不是 future 而是 Event：占位必须在提交之前完成，否则消费者能
        # 在「已提交但还没登记」的缝里钻过去，重复下载。
        self._inflight: dict[int, threading.Event] = {}
        self._pf_pending: dict[int, int] = {}
        self._last_end = 0  # 上一次请求的结束位置，用来判断是否在顺序前进

    # -- 内部 ----------------------------------------------------------------
    def _fetch(self, offset: int, n: int) -> bytes:
        end = min(offset + n, self.size) - 1
        if end < offset:
            return b""
        if self.limiter is not None:
            self.limiter.wait(n)
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

        with self._lock:
            self.bytes += len(data)
            self.requests += 1
            if self.range_log is not None and len(self.range_log) < RANGE_LOG_CAP:
                self.range_log.append((self.index, offset, len(data)))
        return data

    def _block(
        self,
        index: int,
        *,
        prefetch: bool = False,
        claim: threading.Event | None = None,
    ) -> bytes:
        """取一个块。同一块**永远只下载一次**，无论多少线程同时要它。

        ``claim`` 是调用者已经持有的「取块权」（预读线程用它把自己登记过的
        格子交进来）。没有它就得现抢：抢到的人负责取，没抢到的人等。
        """
        start = index * self.block
        if start >= self.size:
            return b""
        want = min(self.block, self.size - start)

        while True:
            with self._lock:
                blk = self._cache.get(index)
                if blk is not None and len(blk) >= want:
                    self._cache.move_to_end(index)
                    pending = self._pf_pending.pop(index, 0)
                    mine = None
                else:
                    blk = None
                    pending = 0
                    if claim is not None:
                        mine = claim
                    else:
                        ev = self._inflight.get(index)
                        if ev is None:
                            ev = threading.Event()
                            self._inflight[index] = ev
                            mine = ev
                        else:
                            mine = None  # 有人正在取，等它，别重复下载
            if blk is not None:
                # 预读来的块被真的读到了——这部分提前量是赚的。
                if pending and self.prefetcher is not None:
                    self.prefetcher.note_used(pending)
                return blk
            if mine is None:
                ev.wait()
                continue

            # 锁外取数：这是并发唯一能发生的地方。
            try:
                blk = self._fetch(start, want)
            except BaseException:
                # 取数失败也要放掉登记并唤醒等待者，否则它们会一直等下去。
                # 被唤醒的人会自己重试，然后如实地把错误报出来。
                self._release(index, mine, owner=claim is None)
                raise

            with self._lock:
                self._cache[index] = blk
                self._cache.move_to_end(index)
                if prefetch:
                    self._pf_pending[index] = len(blk)
                while self.cache_blocks > 0 and len(self._cache) > self.cache_blocks:
                    evicted, _data = self._cache.popitem(last=False)
                    # 预读来的块还没被读就被挤掉：这块流量白花了，如实记账。
                    stale = self._pf_pending.pop(evicted, 0)
                    if stale and self.prefetcher is not None:
                        self.prefetcher.note_wasted(stale)
            # 顺序要紧：块先落进缓存，再唤醒等待者。反过来的话，被唤醒的人
            # 会看见「没人取、缓存也没有」，于是重复下载同一个块。
            self._release(index, mine, owner=claim is None)
            return blk

    def _release(self, index: int, ev: threading.Event, *, owner: bool) -> None:
        """交还取块权。``owner`` 为假时（预读线程持有 claim）由调用者负责。"""
        if not owner:
            return
        with self._lock:
            self._inflight.pop(index, None)
        ev.set()

    def _prefetch_block(self, index: int, claim: threading.Event) -> None:
        """预读线程的入口。失败不重要——真正要读时会重试并如实报错。

        ``claim`` 的登记与释放都由这里负责，``_block`` 只管取数。这样无论
        是正常完成、取数报错、还是收尾时被跳过，等待者都一定会被唤醒。
        """
        try:
            if not self._closed:
                self._block(index, prefetch=True, claim=claim)
        except Exception:  # noqa: BLE001 - 预读失败绝不该影响安装
            pass
        finally:
            with self._lock:
                self._inflight.pop(index, None)
            claim.set()

    def _maybe_prefetch(self, offset: int, n: int) -> None:
        """顺序前进时，把后面 ``jobs - 1`` 个块投出去。

        只在**前向**访问时预读：中央目录解析会在卷尾来回跳，那里的块多半
        再也不会被读，预读纯属浪费流量。
        """
        pf = self.prefetcher
        end = offset + n
        forward = offset >= self._last_end
        self._last_end = max(end, self._last_end) if forward else end
        if pf is None or not pf.enabled or not forward or self._closed:
            return

        first = end // self.block
        plan: list[tuple[int, threading.Event]] = []
        with self._lock:
            for k in range(first, first + pf.jobs - 1):
                if k * self.block >= self.size:
                    break
                if k in self._cache or k in self._inflight:
                    continue
                ev = threading.Event()
                self._inflight[k] = ev
                plan.append((k, ev))
        for k, ev in plan:
            if pf.submit(self._prefetch_block, k, ev) is None:
                # 池已关：撤掉登记并唤醒可能的等待者，否则它们会挂死。
                with self._lock:
                    self._inflight.pop(k, None)
                ev.set()

    # -- Volume 协议 ---------------------------------------------------------
    def read_at(self, offset: int, n: int) -> bytes:
        if n <= 0 or offset >= self.size:
            return b""
        offset = max(0, offset)
        n = min(n, self.size - offset)
        if n <= 0:
            return b""

        if self.cache_blocks <= 0:
            return self._fetch(offset, n)

        out = bytearray()
        pos = offset
        end = offset + n
        while pos < end:
            base = (pos // self.block) * self.block
            blk = self._block(pos // self.block)
            if not blk:
                break
            chunk = blk[pos - base : end - base]
            if not chunk:
                break
            out += chunk
            pos += len(chunk)

        if len(out) == n:
            self._maybe_prefetch(offset, n)
        return bytes(out)

    def flush_stats(self) -> None:
        """把还没被消费的预读块计入浪费（收尾时调用）。"""
        with self._lock:
            left = sum(self._pf_pending.values())
            self._pf_pending.clear()
        if left and self.prefetcher is not None:
            self.prefetcher.note_wasted(left)

    def close(self) -> None:
        self.flush_stats()
        self._closed = True
        with self._lock:
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
    "Prefetcher",
    "Volume",
    "FileVolume",
    "MissingVolume",
    "HttpVolume",
    "ConcatReader",
    "open_stream",
]
