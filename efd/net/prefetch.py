"""顺序预读：把「顺序读取」里每次请求的建连与握手延迟叠起来。

预读只对**前向顺序读**有意义——安装计划本来就按偏移排好序，所以下一个
要读的区间可以精确预测。它不是「并行下载」：读的仍是同一条顺序路径。"""

from __future__ import annotations

import concurrent.futures
import threading

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
