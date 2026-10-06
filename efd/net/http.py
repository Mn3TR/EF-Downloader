"""HTTP Range 分卷：块缓存 + 顺序预读 + 传输层重试。

必须保留的细节：

* **块缓存**：中央目录的解析会在末尾反复小幅回看，缓存能把请求数压下来。
  它同时也是顺序预读的落点——请求先对齐到块，块才能被提前取来复用。
* **206 校验**：若 CDN 不再支持 Range 而返回 200 全文件，偏移量会全错。
  静默接受这种响应会写出损坏的文件，所以宁可立刻报错。
* **锁只护住缓存和计数**：网络请求一律在锁外发，否则预读线程会互相排队，
  并发就等于没加。"""

from __future__ import annotations

import http.client
import threading
import time
import urllib.request

from collections import OrderedDict

from ..core.config import DEFAULT_TIMEOUT, USER_AGENT
from .prefetch import Prefetcher

RANGE_LOG_CAP = 200

# 传输层抖动要重试。抖动**不是**「这个文件下不了」：实测 400 次 Range 请求里
# 就有 1 次 ``ssl.SSLEOFError: UNEXPECTED_EOF_WHILE_READING``（0.25%）。单看
# 很小，但一个 1.1 GiB 的文件要发上千次请求，不重试的话每次安装都几乎必然
# 死在某个大文件半路——用户看到的就是「后面下载大文件时提前提示下载完成」。
FETCH_ATTEMPTS = 3
FETCH_BACKOFF = 0.5


class RangeError(RuntimeError):
    """Range 请求没被正确满足。"""


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

        for attempt in range(1, FETCH_ATTEMPTS + 1):
            try:
                return self._fetch_once(offset, end)
            except (OSError, http.client.HTTPException):
                # 连接被重置、TLS 握手中途被掐、代理断流、响应提前收流……
                # 这些都会自愈，隔一会儿原样重发即可。最后一次仍然失败才往
                # 外抛，让安装如实记下这个文件失败。
                if attempt >= FETCH_ATTEMPTS:
                    raise
                time.sleep(FETCH_BACKOFF * attempt)
        raise AssertionError("不可能到这里")  # pragma: no cover

    def _fetch_once(self, offset: int, end: int) -> bytes:
        want = end - offset + 1
        req = urllib.request.Request(
            self.url,
            headers={"User-Agent": USER_AGENT, "Range": f"bytes={offset}-{end}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            status = getattr(resp, "status", None)
            data = resp.read()

        whole_file_ok = offset == 0 and want >= self.size
        if status not in (None, 206) and not whole_file_ok:
            raise RangeError(
                f"卷 {self.index:03d} 对 Range bytes={offset}-{end} 返回了 {status}"
                f"（期望 206）。CDN 可能已不支持范围请求。"
            )
        if len(data) > want and not whole_file_ok:
            raise RangeError(
                f"卷 {self.index:03d} 期望 {want} 字节却收到 {len(data)} 字节"
            )
        if len(data) < want:
            # 头部承诺的长度没兑现：算传输层抖动，值得重试（否则这个短块会
            # 一直留在缓存里，让后面每一次读都重新发一遍请求）。
            raise http.client.IncompleteRead(data, want - len(data))

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
