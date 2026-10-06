"""预读在一个**有空洞**的计划上会白读多少。

bench_prefetch 量的是「一整段连续读」，那是全新安装的样子。但续装时
plan.need 只含缺失的文件，偏移量是**断断续续**的；预读不看计划、只管
往后取块，跨过空洞时取来的块没人要——这就是白读。

这个脚本用真实清单 + 真实本地目录算出计划，再逐卷模拟预读，把
「jobs 取多少」变成有数据支撑的决定，而不是拍脑袋。**纯计算，不下载。**

    python tools/bench_gaps.py

2026-10 在本机实测（1601 条清单里 1061 条待装，即已经装了一部分）：

    jobs=2   白读  53 MB / 0.1%
    jobs=4   白读 159 MB / 0.3%
    jobs=8   白读 371 MB / 0.7%   ← DEFAULT_JOBS
    jobs=16  白读 795 MB / 1.5%

全新安装没有空洞，白读为 0。
"""

from __future__ import annotations

import bisect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from efd import detect  # noqa: E402
from efd.core.archive import open_remote  # noqa: E402
from efd.core.planner import make_plan  # noqa: E402
from efd.core.util import human  # noqa: E402

BLOCK = 1 << 20


def blocks_of(entries, starts, volume_sizes) -> list[set[int]]:
    """把「全局偏移 + 长度」的条目映射成每卷各需要哪些块。"""
    need = [set() for _ in volume_sizes]
    for entry in entries:
        pos, left = entry.offset, entry.compressed
        while left > 0:
            v = bisect.bisect_right(starts, pos) - 1
            if v < 0 or v >= len(volume_sizes):
                break
            inner = pos - starts[v]
            take = min(left, volume_sizes[v] - inner)
            if take <= 0:
                break
            need[v].update(range(inner // BLOCK, (inner + take - 1) // BLOCK + 1))
            pos += take
            left -= take
    return need


def simulate(needed: list[set[int]], jobs: int) -> tuple[int, int]:
    """返回 (真读的块数, 白读的块数)。

    消费者按块号递增前进；每读一块就把后面 jobs-1 块投出去。已经在
    缓存里（也就是已经被预读过的）不重复投。
    """
    read_blocks = 0
    wasted: set[tuple[int, int]] = set()
    cached: set[tuple[int, int]] = set()

    for vol, blocks in enumerate(needed):
        for b in sorted(blocks):
            read_blocks += 1
            cached.discard((vol, b))
            for k in range(b + 1, b + jobs):
                key = (vol, k)
                if k in blocks or key in cached or key in wasted:
                    continue
                wasted.add(key)
                cached.add(key)
    return read_blocks, len(wasted)


def main() -> int:
    target = detect.suggest_target()
    if not target:
        print("没找到游戏目录，改用「全新安装」模型（无空洞）。")
        target = ""
    else:
        print(f"目标目录：{target}")

    with open_remote() as archive:
        sizes = [v.size for v in archive.volumes]
        starts, acc = [], 0
        for size in sizes:
            starts.append(acc)
            acc += size

        plan = make_plan(archive, target) if target else None
        entries = list(plan.need) if plan else list(archive.files())
        total_files = len(list(archive.files()))
        print(f"清单 {total_files} 个文件；本次待装 {len(entries)} 个"
              f"（已有 {total_files - len(entries)} 个）\n")

        needed = blocks_of(entries, starts, sizes)
        total_blocks = sum(len(s) for s in needed)
        exact_bytes = sum(e.compressed for e in entries)

    # 读取是按 1 MiB 块对齐的（预读的落点就是块），所以实际下载量是
    # 「被覆盖的块数 × 块大小」，比按条目累加的字节数略多。多出来的部分
    # 出现在每个不连续区间的两端。这里把这份开销也一并量出来。
    aligned_bytes = total_blocks * BLOCK
    overhead = aligned_bytes - exact_bytes
    print(f"按条目精确下载   = {human(exact_bytes)}")
    print(f"按 1 MiB 块对齐   = {human(aligned_bytes)}"
          f"   （多 {human(overhead)}，"
          f"{overhead / max(1, exact_bytes):.2%}）\n")

    print(f"{'jobs':>5} {'真读块':>10} {'白读块':>10} {'白读占比':>9} {'白读字节':>12}")
    print("-" * 52)
    for jobs in (1, 2, 4, 8, 12, 16):
        read_blocks, wasted_blocks = simulate(needed, jobs)
        share = wasted_blocks / max(1, read_blocks + wasted_blocks)
        print(f"{jobs:>5} {read_blocks:>10,} {wasted_blocks:>10,} "
              f"{share:>8.1%} {human(wasted_blocks * BLOCK):>12}")
    print("-" * 52)
    print(f"计划覆盖 {total_blocks:,} 个块 / {human(aligned_bytes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
