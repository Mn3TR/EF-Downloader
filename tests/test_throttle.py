"""限速与并发数的解析、以及令牌桶本身的行为。

限速的可测之处在于**预约**是纯计算：``reserve()`` 在锁内算好出发时刻就返回，
不真的睡。所以下面的时间断言都打在预约上，测试因此是确定性的、也不慢。
"""

from __future__ import annotations

import threading
import time
import unittest

from efd.throttle import (
    RateError,
    RateLimiter,
    check_jobs,
    format_rate,
    parse_rate,
)


class TestParseRate(unittest.TestCase):
    def test_plain_number_is_bytes_per_second(self):
        self.assertEqual(parse_rate("1024"), 1024.0)

    def test_suffixes_use_1024(self):
        self.assertEqual(parse_rate("8K"), 8 * 1024)
        self.assertEqual(parse_rate("8M"), 8 * (1 << 20))
        self.assertEqual(parse_rate("1G"), 1 << 30)

    def test_fractional_rate(self):
        self.assertEqual(parse_rate("1.5M"), 1.5 * (1 << 20))

    def test_case_and_suffix_noise_are_ignored(self):
        self.assertEqual(parse_rate("8m"), 8 * (1 << 20))
        self.assertEqual(parse_rate("  512K  "), 512 * 1024)
        self.assertEqual(parse_rate("8MB"), 8 * (1 << 20))

    def test_zero_and_blank_mean_no_limit(self):
        for text in ("0", "", "   ", None):
            self.assertEqual(parse_rate(text), 0.0)

    def test_garbage_is_rejected_loudly(self):
        # 写错了必须当场报错：静默当一个默认值会让人以为限了速其实没限。
        with self.assertRaises(RateError):
            parse_rate("快一点")

    def test_negative_is_rejected(self):
        with self.assertRaises(RateError):
            parse_rate("-1M")


class TestFormatRate(unittest.TestCase):
    def test_zero_reads_as_no_limit(self):
        self.assertEqual(format_rate(0), "不限速")

    def test_round_trip_through_parse(self):
        for text in ("8M", "512K", "1G"):
            rate = parse_rate(text)
            self.assertEqual(parse_rate(format_rate(rate)), rate)

    def test_small_rate_falls_back_to_bytes(self):
        self.assertIn("B/s", format_rate(999))


class TestCheckJobs(unittest.TestCase):
    def test_one_is_allowed_and_means_no_prefetch(self):
        self.assertEqual(check_jobs(1), 1)

    def test_typical_values_pass_through(self):
        self.assertEqual(check_jobs(4), 4)
        self.assertEqual(check_jobs(32), 32)

    def test_zero_is_rejected(self):
        with self.assertRaises(RateError):
            check_jobs(0)

    def test_absurd_values_are_rejected(self):
        with self.assertRaises(RateError):
            check_jobs(33)
        with self.assertRaises(RateError):
            check_jobs(-4)

    def test_non_integer_is_rejected(self):
        with self.assertRaises(RateError):
            check_jobs("很多")


class TestReserve(unittest.TestCase):
    def test_disabled_limiter_never_waits(self):
        limiter = RateLimiter(0)
        self.assertFalse(limiter.enabled)
        for _ in range(5):
            self.assertEqual(limiter.reserve(1 << 20), 0.0)

    def test_first_request_goes_immediately(self):
        # 空闲后不该积攒突发，也不该先罚一段等待。
        limiter = RateLimiter(1 << 20)
        self.assertEqual(limiter.reserve(1 << 20), 0.0)

    def test_second_request_waits_one_transmission_time(self):
        limiter = RateLimiter(1 << 20)  # 1 MiB/s
        limiter.reserve(1 << 20)  # 用掉 1 秒的管道时间
        delay = limiter.reserve(1 << 20)
        self.assertAlmostEqual(delay, 1.0, delta=0.05)

    def test_delay_scales_with_size(self):
        limiter = RateLimiter(1 << 20)
        limiter.reserve(0)  # 不影响队列
        self.assertAlmostEqual(limiter.reserve(1 << 19), 0.0, delta=0.05)
        # 半兆已经预约了 0.5 秒，再要半兆得再等 0.5 秒。
        self.assertAlmostEqual(limiter.reserve(1 << 19), 0.5, delta=0.05)

    def test_reserving_without_waiting_still_keeps_the_schedule(self):
        """连续预约但不睡：返回值是「距离你的时隙还有多久」，因此会累积。

        这条断言的是「账没算丢」——8 块 1 秒的活全排进队列后，
        最后一块要等 7 秒。真实的等待发生在 ``wait()`` 里，见 TestConcurrency。
        """
        rate = 1 << 20
        limiter = RateLimiter(rate)
        blocks = 8
        delays = [limiter.reserve(1 << 20) for _ in range(blocks)]
        self.assertEqual(delays[0], 0.0)
        for i, delay in enumerate(delays):
            self.assertAlmostEqual(delay, float(i), delta=0.05)

    def test_idle_time_is_not_banked(self):
        """空闲很久之后再来请求，不该因为「攒了额度」而放出一串突发。"""
        limiter = RateLimiter(1 << 20)
        limiter.reserve(1 << 20)
        time.sleep(0.05)  # 远不到 1 秒的额度
        # 队列仍在未来，所以还是得等——但只等剩余的部分，不是重新计一整秒。
        delay = limiter.reserve(1 << 20)
        self.assertLess(delay, 1.05)
        self.assertGreater(delay, 0.5)

    def test_raising_the_rate_takes_effect(self):
        limiter = RateLimiter(1 << 20)
        limiter.reserve(1 << 20)
        limiter.rate = 0  # 解除限速
        self.assertEqual(limiter.reserve(1 << 20), 0.0)

    def test_zero_size_is_free(self):
        limiter = RateLimiter(1 << 20)
        self.assertEqual(limiter.reserve(0), 0.0)
        self.assertEqual(limiter.reserve(-5), 0.0)


class TestConcurrency(unittest.TestCase):
    def test_reservations_from_many_threads_never_overlap(self):
        """并发预约必须串行排队：这是限速在多线程下成立的前提。"""
        rate = 1 << 20
        limiter = RateLimiter(rate)
        blocks = 16
        started = time.monotonic()

        def worker():
            limiter.wait(1 << 20)

        threads = [threading.Thread(target=worker) for _ in range(blocks)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - started

        # 16 块、每块 1 秒的管道时间，第一块立刻走 → 至少约 15 秒。
        # 下限给得保守些，避免慢机器抖动；上限放宽以免误报。
        self.assertGreater(elapsed, 12.0)
        self.assertLess(elapsed, 25.0)

    def test_waited_time_is_accumulated_for_display(self):
        limiter = RateLimiter(64 * (1 << 20))  # 快到几乎不等
        limiter.wait(1 << 20)
        limiter.wait(1 << 20)
        self.assertGreaterEqual(limiter.waited, 0.0)


if __name__ == "__main__":
    unittest.main()
