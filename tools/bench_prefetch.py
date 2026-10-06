"""实测：顺序预读到底快不快，以及 jobs 取多少合适。

单流 1 MiB 一请求的做法，每个请求都要重新建连（含 TLS 握手），
请求-响应之间的空档全在等 RTT。预读的假设是：这段空档可以叠起来。

但假设必须量。这里的做法是**同一卷、同一段区间、依次换成不同的 jobs**，
把网络条件的影响压到最小。每轮之间间隔几秒，避免 CDN 侧把前一轮
的限流带到下一轮。

这个脚本量的是「一整段连续读」，也就是全新安装的样子。续装时计划里有空洞，
预读会白读一些——那部分由 ``tools/bench_gaps.py``（纯计算，不联网）负责。

    python tools/bench_prefetch.py            # 默认每档 64 MiB
    python tools/bench_prefetch.py 128        # 每档 128 MiB

2026-10 在本机实测（卷 001，64 MiB/档，每档两轮取较快）：

    jobs=1   2.74 MB/s   1.00×
    jobs=4   3.58 MB/s   1.30×
    jobs=8   3.89 MB/s   1.42×   ← 拐点，throttle.DEFAULT_JOBS 取的就是它
    jobs=16  3.60 MB/s   1.31×
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from efd.core import seed as seed_mod  # noqa: E402
from efd.core.throttle import RateLimiter  # noqa: E402
from efd.net import HttpVolume, Prefetcher  # noqa: E402
from efd.core.util import human  # noqa: E402

SPAN = int(sys.argv[1]) if len(sys.argv) > 1 else 64
BLOCK = 1 << 20
WARMUP = 2 * BLOCK  # 先热身两兆，把 DNS/TLS 建连的开销从计时里排除


def run(pack, jobs: int, span: int) -> tuple[float, int, int, int, int]:
    """返回 (秒, 下载字节, 请求数, 白读字节, 用上字节)。"""
    pf = Prefetcher(jobs) if jobs > 1 else None
    vol = HttpVolume(pack.index, pack.url, pack.size, block=BLOCK,
                     prefetcher=pf, limiter=None)
    try:
        # 热身：不算时间。
        vol.read_at(0, WARMUP)
        before_bytes, before_reqs = vol.bytes, vol.requests

        start = time.monotonic()
        off = WARMUP
        end = min(span, vol.size)
        while off < end:
            n = min(BLOCK, end - off)
            got = vol.read_at(off, n)
            if len(got) != n:
                raise RuntimeError(f"短读：要 {n} 拿到 {len(got)}")
            off += n
        elapsed = time.monotonic() - start
    finally:
        vol.close()
        if pf is not None:
            pf.shutdown()

    used = pf.fetched if pf is not None else 0
    wasted = pf.wasted if pf is not None else 0
    return elapsed, vol.bytes - before_bytes, vol.requests - before_reqs, wasted, used


def main() -> int:
    rel = seed_mod.fetch_release()
    pack = max(rel.packs, key=lambda p: p.size)
    print(f"包版本 {rel.version}，共 {len(rel.packs)} 卷")
    print(f"选用卷 {pack.index:03d}（{human(pack.size)}），"
          f"每档读 {SPAN} MiB，块 = 1 MiB\n")

    span = min(SPAN * (1 << 20), pack.size)
    results: dict[int, list[float]] = {}

    # 每档跑两轮取较快的一轮：慢的那轮多半是被 CDN 侧限流砸中，
    # 取较快值更接近这条链路真实能跑到的上限。
    for jobs in (1, 4, 8, 16):
        speeds: list[float] = []
        for attempt in range(2):
            elapsed, got, reqs, wasted, used = run(pack, jobs, span)
            speed = got / elapsed / (1 << 20)
            speeds.append(speed)
            note = f"  白读 {human(wasted)} / 用上 {human(used)}" if jobs > 1 else ""
            print(f"  jobs={jobs:>2}  第{attempt + 1}轮  {elapsed:6.2f}s  "
                  f"{speed:5.2f} MB/s  请求 {reqs:>4}{note}")
            time.sleep(3)
        results[jobs] = speeds
        best = max(speeds)
        base = max(results[1])
        print(f"  → jobs={jobs:>2} 取较快 {best:5.2f} MB/s"
              f"（相对 jobs=1 {base:5.2f}：{best / base:.2f}×）\n")

    print("=" * 62)
    base = max(results[1])
    for jobs in sorted(results):
        best = max(results[jobs])
        print(f"  jobs={jobs:>2}   {best:5.2f} MB/s   {best / base:5.2f}×   中位 {statistics.median(results[jobs]):5.2f}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
