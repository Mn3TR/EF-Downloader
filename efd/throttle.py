"""限速：把下载速率压到一个上限以下。

用「虚拟时间槽」而不是「定期补充令牌的桶」：

* 每个请求在锁内预约一段独占的管道时间 ``n / rate``，得到自己的出发时刻；
* 预约是**单调**的，所以任意窗口内的平均速率天然不超过 ``rate``；
* 空闲一段时间后再来请求，出发时刻就是「现在」，**没有积攒的突发**。

令牌桶那种实现要么在空闲后放出一大串突发流量，要么需要额外的后台线程定期补令牌。
这里两个问题都不存在，也不需要任何线程来推动时间。
"""

from __future__ import annotations

import threading
import time

# 速率字符串的进制：与 util.human() 保持一致，1K = 1024。
_SCALE = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}

# 并发数的边界放在这里，CLI 和 GUI 共用同一套，免得两边说法不一致。
MIN_JOBS = 1
MAX_JOBS = 32

# 默认并发。这是 **实测出来的**，不是拍的：
#   tools/bench_prefetch.py 同一卷同一段依次换 jobs，每档两轮取较快值 ——
#     jobs=1  2.74 MB/s   1.00×
#     jobs=4  3.58 MB/s   1.30×
#     jobs=8  3.89 MB/s   1.42×   ← 拐点
#     jobs=16 3.60 MB/s   1.31×   （再多反而回落）
#   tools/bench_gaps.py 在真实「续装」计划（1601 条里 1061 条待装）上量白读 ——
#     jobs=8 白读 371 MB / 52.52 GB = 0.7%；全新安装时是 0。
# 所以 8：速度拿满，代价不到百分之一。
DEFAULT_JOBS = 8


class RateError(ValueError):
    """速率或并发数写得不对。"""


class RateLimiter:
    """线程安全的速率限制器。``rate <= 0`` 表示不限速。

    同一个实例要由**所有** HttpVolume（包括预读线程）共享，否则每个对象各自
    限速，总速率会随并发数翻倍。
    """

    def __init__(self, rate: float = 0.0):
        self._rate = float(rate)
        self._lock = threading.Lock()
        self._next = 0.0  # 管道下一次空闲的时刻（time.monotonic 坐标系）
        self.waited = 0.0  # 累计因限速睡掉的时间，供界面显示

    @property
    def rate(self) -> float:
        return self._rate

    @rate.setter
    def rate(self, value: float) -> None:
        with self._lock:
            self._rate = float(value)

    @property
    def enabled(self) -> bool:
        return self._rate > 0

    def reserve(self, n: int) -> float:
        """预约 ``n`` 字节的传输时间，返回调用方还需要等待的秒数。

        只算不睡，便于测试；真正要阻塞时用 :meth:`wait`。
        """
        if self._rate <= 0 or n <= 0:
            return 0.0
        now = time.monotonic()
        with self._lock:
            start = now if now > self._next else self._next
            self._next = start + n / self._rate
            delay = start - now
        return delay

    def wait(self, n: int) -> None:
        """按预约阻塞到该自己發車。"""
        delay = self.reserve(n)
        if delay > 0:
            self.waited += delay
            time.sleep(delay)


def parse_rate(text: str) -> float:
    """把 ``8M`` / ``512K`` / ``1.5M`` / ``0`` 解析成字节每秒。

    ``0``（以及空串）表示不限速。进制按 1024。
    """
    if text is None:
        return 0.0
    raw = str(text).strip().upper().replace("B/S", "").replace("B", "")
    if not raw:
        return 0.0
    suffix = ""
    if raw[-1] in _SCALE and raw[-1] != "":
        suffix, raw = raw[-1], raw[:-1]
    try:
        value = float(raw)
    except ValueError:
        raise RateError(f"看不懂的速率：{text!r}（示例：8M、512K、0 表示不限）") from None
    if value < 0:
        raise RateError(f"速率不能是负数：{text!r}")
    return value * _SCALE[suffix]


def format_rate(rate: float) -> str:
    """反向显示，给界面和日志用。"""
    if rate <= 0:
        return "不限速"
    for unit, scale in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if rate >= scale:
            return f"{rate / scale:g} {unit}B/s"
    return f"{rate:g} B/s"


def check_jobs(jobs: int) -> int:
    """并发数的合法区间。1 就是老行为（不预读）。"""
    try:
        value = int(jobs)
    except (TypeError, ValueError):
        raise RateError(f"并发数必须是整数：{jobs!r}") from None
    if value < MIN_JOBS:
        raise RateError(f"并发数至少是 {MIN_JOBS}（提交的是 {value}）")
    if value > MAX_JOBS:
        raise RateError(f"并发数最多 {MAX_JOBS}（提交的是 {value}）；再高只会把带宽切得更碎")
    return value


__all__ = [
    "DEFAULT_JOBS",
    "MAX_JOBS",
    "MIN_JOBS",
    "RateError",
    "RateLimiter",
    "parse_rate",
    "format_rate",
    "check_jobs",
]
