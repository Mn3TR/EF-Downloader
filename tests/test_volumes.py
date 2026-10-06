"""ConcatReader 的边界测试（纯内存，无网络）。

跨卷读取是整个方案的地基：偏移量算错一点点，解压出来的就是垃圾。
"""

from __future__ import annotations

import http.client
import io
import ssl
import threading
import time
import unittest
import urllib.error
from unittest import mock

from efd.net import (
    ConcatReader,
    HttpVolume,
    MissingVolume,
    Prefetcher,
    RangeError,
    open_stream,
)


class MemoryVolume:
    """内存分卷，用于测试。"""

    def __init__(self, data: bytes, index: int = 0):
        self.data = data
        self.size = len(data)
        self.index = index

    def read_at(self, offset: int, n: int) -> bytes:
        return self.data[offset : offset + n]

    def close(self) -> None:
        pass


class ShortVolume(MemoryVolume):
    """谎报尺寸、实际给不出数据的坏卷。"""

    def read_at(self, offset: int, n: int) -> bytes:
        return b""


def reader(*chunks: bytes) -> ConcatReader:
    return ConcatReader([MemoryVolume(c, i) for i, c in enumerate(chunks)])


class TestConcatReader(unittest.TestCase):
    def test_total(self):
        self.assertEqual(reader(b"abc", b"de", b"f").total, 6)

    def test_sequential_read(self):
        r = reader(b"abc", b"de", b"f")
        self.assertEqual(r.read(), b"abcde" + b"f")

    def test_read_across_boundary(self):
        r = reader(b"abc", b"defg")
        self.assertEqual(r.read(5), b"abcde")

    def test_bounded_reads_walk_correctly(self):
        r = reader(b"ab", b"cd", b"ef")
        self.assertEqual([r.read(1) for _ in range(6)], [b"a", b"b", b"c", b"d", b"e", b"f"])
        self.assertEqual(r.read(1), b"")

    def test_read_single_byte_spans_many_volumes(self):
        r = reader(b"a", b"b", b"c", b"d", b"e")
        self.assertEqual(r.read(5), b"abcde")

    def test_seek_set_cur_end(self):
        r = reader(b"abcdef")
        self.assertEqual(r.seek(2), 2)
        self.assertEqual(r.read(1), b"c")
        self.assertEqual(r.seek(1, io.SEEK_CUR), 4)
        self.assertEqual(r.read(1), b"e")
        self.assertEqual(r.seek(-1, io.SEEK_END), 5)
        self.assertEqual(r.read(1), b"f")

    def test_seek_clamps(self):
        r = reader(b"abc")
        self.assertEqual(r.seek(-100), 0)
        self.assertEqual(r.seek(999), 3)
        self.assertEqual(r.read(), b"")

    def test_seek_after_end_then_read_is_empty(self):
        r = reader(b"abc", b"def")
        r.seek(10)
        self.assertEqual(r.read(), b"")

    def test_invalid_whence(self):
        with self.assertRaises(ValueError):
            reader(b"abc").seek(0, 99)

    def test_read_none_and_negative(self):
        r = reader(b"abc")
        self.assertEqual(r.read(None), b"abc")
        r.seek(0)
        self.assertEqual(r.read(-1), b"abc")

    def test_tell(self):
        r = reader(b"abc", b"def")
        r.read(4)
        self.assertEqual(r.tell(), 4)

    def test_readinto(self):
        r = reader(b"abc", b"def")
        buf = bytearray(4)
        self.assertEqual(r.readinto(buf), 4)
        self.assertEqual(bytes(buf), b"abcd")

    def test_readinto_at_eof(self):
        r = reader(b"abc")
        r.seek(0)
        r.read(3)
        self.assertEqual(r.readinto(bytearray(4)), 0)

    def test_truncated_volume_raises(self):
        r = ConcatReader([ShortVolume(b"x" * 10)])
        with self.assertRaises(RuntimeError):
            r.read(4)

    def test_missing_volume_raises_and_keeps_offsets(self):
        """缺失分卷仍要贡献正确的偏移量——离线夹具就靠这个。"""
        r = ConcatReader([MissingVolume(1000, 1), MemoryVolume(b"tail", 2)])
        self.assertEqual(r.total, 1004)
        self.assertEqual(r.seek(1000), 1000)
        self.assertEqual(r.read(4), b"tail")
        r.seek(0)
        with self.assertRaises(RuntimeError):
            r.read(1)

    def test_is_rawio_and_buffered_wraps(self):
        stream = open_stream([MemoryVolume(b"hello world")], buffer_size=4)
        try:
            self.assertIsInstance(stream.raw, ConcatReader)
            self.assertEqual(stream.read(), b"hello world")
        finally:
            stream.close()

    def test_close_closes_volumes(self):
        r = reader(b"abc")
        r.close()
        self.assertTrue(r.closed)


class TestPrefetch(unittest.TestCase):
    """预读的正确性：可以白读，但**绝不能重复下载**，也绝不能挂死。

    全部走假 HTTP，不发真请求。假 CDN 记账每一次 Range 调用，所以
    「同一块下载了两遍」这种错误会直接体现为请求次数不对。
    """

    def setUp(self):
        self.calls: list[tuple[int, int]] = []
        self.server = b""

        def fake_urlopen(req, timeout=None):
            rng = req.get_header("Range")
            start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
            self.calls.append((start, end))
            body = self.server[start : end + 1]
            return FakeResponse(body)

        patcher = mock.patch("efd.net.http.urllib.request.urlopen", fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def volume(self, size: int, *, jobs: int = 1, block: int = 16, **kw):
        self.server = bytes(range(256)) * (size // 256 + 1)
        self.server = self.server[:size]
        pf = Prefetcher(jobs) if jobs > 1 else None
        self.addCleanup(lambda: pf and pf.shutdown())
        vol = HttpVolume(0, "http://fake/vol", size, block=block,
                         prefetcher=pf, cache_blocks=kw.pop("cache_blocks", 8), **kw)
        self.addCleanup(vol.close)
        return vol, pf

    def ranges(self) -> list[tuple[int, int]]:
        return list(self.calls)

    def test_sequential_reads_fetch_each_block_once(self):
        vol, _ = self.volume(64, jobs=1, block=16)
        for off in range(0, 64, 16):
            vol.read_at(off, 16)
        self.assertEqual(len(self.calls), 4)

    def test_repeated_read_of_same_block_hits_cache(self):
        vol, _ = self.volume(64, jobs=1, block=16)
        vol.read_at(0, 16)
        vol.read_at(0, 16)
        vol.read_at(8, 8)  # 同块内的另一段，仍应命中缓存
        self.assertEqual(len(self.calls), 1)

    def test_prefetch_does_not_double_download_the_block_it_prefetched(self):
        """最重要的不变量：消费者读到的正是预读取回的那一份。"""
        vol, _ = self.volume(256, jobs=4, block=16)
        for off in range(0, 256, 16):
            vol.read_at(off, 16)
        starts = [s for s, _ in self.calls]
        self.assertEqual(len(starts), len(set(starts)), f"有块被下载了两次：{starts}")

    def test_prefetch_actually_runs_ahead(self):
        vol, pf = self.volume(256, jobs=4, block=16)
        vol.read_at(0, 16)
        self.assertTrue(pf.enabled)
        # 预读是真花了流量的——只在「用上」时才计数，所以这里看请求数。
        deadline = time.monotonic() + 5.0
        while len(self.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertGreaterEqual(len(self.calls), 2, "预读没有投出任何请求")

    def test_jobs_one_never_prefetches(self):
        vol, pf = self.volume(256, jobs=1, block=16)
        for off in range(0, 256, 16):
            vol.read_at(off, 16)
        self.assertEqual(len(self.calls), 16, "jobs=1 时不该有任何预读")

    def test_backward_seek_does_not_prefetch(self):
        """中央目录解析会在卷尾来回跳，那里预读纯属浪费流量。"""
        vol, _ = self.volume(256, jobs=4, block=16)
        vol.read_at(224, 16)  # 先跳到尾部
        before = len(self.calls)
        vol.read_at(0, 16)  # 再跳回开头：这是回退，不预读
        time.sleep(0.05)
        self.assertEqual(len(self.calls), before + 1)

    def test_prefetch_stops_at_end_of_volume(self):
        vol, _ = self.volume(32, jobs=8, block=16)
        vol.read_at(0, 16)
        time.sleep(0.05)
        # 只有两块，任何请求都不该越过卷尾。
        for start, end in self.calls:
            self.assertLess(start, 32)
            self.assertLess(end, 32)

    def test_no_waste_on_a_clean_sequential_run(self):
        """一路顺序读下来不该有任何白读——缓存下限（jobs+2）就是为这个留的。"""
        vol, pf = self.volume(512, jobs=4, block=16)
        for off in range(0, 512, 16):
            vol.read_at(off, 16)
        vol.flush_stats()
        self.assertEqual(pf.wasted, 0)
        self.assertGreater(pf.fetched, 0)

    def test_skipping_ahead_wastes_the_prefetched_blocks(self):
        """计划有空洞时（续装跳过已装好的文件）预读会白读，必须如实记账。"""
        vol, pf = self.volume(2048, jobs=4, block=16)
        vol.read_at(0, 16)
        self._wait_for_calls(4)  # 等预读把后面 3 块取回来
        # 直接跳到很远的地方：中间那几块再也不会被读了。
        vol.read_at(1600, 16)
        vol.flush_stats()
        self.assertGreater(pf.wasted, 0, "跳过一大段却没记任何浪费")

    def test_blocks_prefetched_but_never_read_count_as_wasted_at_close(self):
        vol, pf = self.volume(512, jobs=4, block=16)
        vol.read_at(0, 16)
        self._wait_for_calls(4)
        vol.close()  # flush_stats 在这里把没消费的算成浪费
        self.assertGreater(pf.wasted, 0)

    def _wait_for_calls(self, count: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while len(self.calls) < count and time.monotonic() < deadline:
            time.sleep(0.005)
        return len(self.calls) >= count

    def test_prefetched_block_is_counted_as_used(self):
        vol, pf = self.volume(256, jobs=4, block=16)
        # 慢一点读，给预读留出提前量，确保有块是「预读来的」。
        for off in range(0, 256, 16):
            vol.read_at(off, 16)
            time.sleep(0.01)
        self.assertGreater(pf.fetched, 0, "没有任何块算作预读命中")

    def test_close_is_idempotent_with_prefetch_running(self):
        vol, _ = self.volume(1024, jobs=4, block=16)
        vol.read_at(0, 16)
        vol.close()
        vol.close()  # 第二次不该炸

    def test_read_after_close_does_not_hang(self):
        vol, _ = self.volume(128, jobs=4, block=16)
        vol.read_at(0, 16)
        vol.close()
        deadline = time.monotonic() + 5.0
        done = threading.Event()

        def reader():
            vol.read_at(16, 16)
            done.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout=5.0)
        self.assertTrue(done.wait(0), "关卷后的读取挂死了")
        self.assertLess(time.monotonic(), deadline)


class TestPrefetcherUnit(unittest.TestCase):
    def test_jobs_one_is_disabled(self):
        pf = Prefetcher(1)
        self.assertFalse(pf.enabled)
        self.assertIsNone(pf.submit(lambda: None))

    def test_jobs_above_one_is_enabled(self):
        pf = Prefetcher(4)
        self.addCleanup(pf.shutdown)
        self.assertTrue(pf.enabled)

    def test_submit_runs_the_callable(self):
        pf = Prefetcher(2)
        self.addCleanup(pf.shutdown)
        done = threading.Event()
        pf.submit(done.set)
        self.assertTrue(done.wait(5.0))

    def test_submit_after_shutdown_returns_none(self):
        """池关了之后提交必须是「静默失败」，让调用者撤登记，而不是抛。"""
        pf = Prefetcher(2)
        pf.shutdown()
        self.assertIsNone(pf.submit(lambda: None))

    def test_stats_are_thread_safe(self):
        pf = Prefetcher(4)
        self.addCleanup(pf.shutdown)

        def bump():
            for _ in range(200):
                pf.note_used(1)
                pf.note_wasted(1)

        threads = [threading.Thread(target=bump) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(pf.fetched, 1600)
        self.assertEqual(pf.wasted, 1600)


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.status = 206

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestFetchRetry(unittest.TestCase):
    """传输层抖动必须自愈。

    现场实测：400 次真实 Range 请求里就有 1 次
    ``URLError: [SSL: UNEXPECTED_EOF_WHILE_READING]``（0.25%）。一个 1.1 GiB
    的大文件要上千次请求，不重试的话几乎必然死在半路——而 0.2.3 之前的表现
    不是「报错」，是 GUI **弹「安装完成」并画到 100%**，所以这组测试盯的是
    「抖动之后还能不能把文件老老实实读完」。
    """

    def setUp(self):
        self.calls: list[tuple[int, int]] = []
        self.body = bytes(range(256)) * 2
        self.fail_times = 0
        self.error: BaseException | None = None
        self.short_by = 0

        def fake_urlopen(req, timeout=None):
            rng = req.get_header("Range")
            start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
            self.calls.append((start, end))
            if len(self.calls) <= self.fail_times:
                raise self.error
            body = self.body[start : end + 1]
            if self.short_by:
                body = body[: max(0, len(body) - self.short_by)]
            return FakeResponse(body)

        patcher = mock.patch("efd.net.http.urllib.request.urlopen", fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 退避只为线上省事，测试里别真的睡。
        backoff = mock.patch("efd.net.http.FETCH_BACKOFF", 0.0)
        backoff.start()
        self.addCleanup(backoff.stop)

    def volume(self, size: int = 32, **kw):
        vol = HttpVolume(0, "http://fake/vol", size, block=16,
                         cache_blocks=kw.pop("cache_blocks", 8), **kw)
        self.addCleanup(vol.close)
        return vol

    def test_transient_ssl_eof_is_retried_and_the_read_succeeds(self):
        self.fail_times = 1
        self.error = urllib.error.URLError(
            ssl.SSLEOFError(8, "EOF occurred in violation of protocol"))
        vol = self.volume()
        self.assertEqual(vol.read_at(0, 16), self.body[:16])
        self.assertEqual(len(self.calls), 2, "抖动之后没有原样重发")

    def test_every_fetch_attempt_is_bounded(self):
        """一直失败时必须如实抛出去，不能无限重试把安装挂死。"""
        from efd.net.http import FETCH_ATTEMPTS

        self.fail_times = 99
        self.error = ConnectionResetError("connection reset by peer")
        vol = self.volume()
        with self.assertRaises(OSError):
            vol.read_at(0, 16)
        self.assertEqual(len(self.calls), FETCH_ATTEMPTS)

    def test_short_read_is_treated_as_jitter(self):
        """头部承诺的长度没兑现：重发，而不是把短块塞进缓存。"""
        self.short_by = 4
        vol = self.volume()
        with self.assertRaises(http.client.IncompleteRead):
            vol.read_at(0, 16)
        self.assertEqual(len(self.calls), 3)

    def test_a_bad_status_is_not_retried(self):
        """服务器不认 Range（回 200）是配置问题，重试三遍只是浪费流量。"""
        self.fail_times = 99
        self.error = RangeError("卷 000 对 Range 返回了 200（期望 206）")
        vol = self.volume()
        with self.assertRaises(RangeError):
            vol.read_at(0, 16)
        self.assertEqual(len(self.calls), 1)

    def test_retries_do_not_double_count_the_download(self):
        """重试期间的失败请求不该计进流量，否则「下载量」会虚高。"""
        self.fail_times = 2
        self.error = urllib.error.URLError("temporary failure in name resolution")
        vol = self.volume()
        vol.read_at(0, 16)
        self.assertEqual(vol.requests, 1)
        self.assertEqual(vol.bytes, 16)


if __name__ == "__main__":
    unittest.main()
