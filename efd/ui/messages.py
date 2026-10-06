"""worker 线程与界面之间的消息协议。

界面从不直接读 worker 的状态，只认队列里的消息。所以「这一轮结束了」
必须有明确的种类——v0.2.0 的检查流程漏发了结束消息，表现就是检查跑完后
【开始安装】永远点不动。
"""

from __future__ import annotations

import queue
import threading
import traceback
from enum import Enum

from ..core import config
from ..core.archive import Archive, open_remote
from ..core.installer import Progress, Result, install
from ..core.planner import Plan, make_plan


class Msg(str, Enum):
    """队列上流动的消息种类。

    继承 ``str`` 是刻意的（``enum.StrEnum`` 要 3.11，本项目底线是 3.10）：
    队列里放的仍是普通字符串（``.value``），这样日志、测试和将来任何
    消费方都不必知道枚举的存在。
    """

    STATUS = "status"
    PLAN = "plan"
    CHECKED = "checked"
    PROGRESS = "progress"
    DONE = "done"
    STOPPED = "stopped"
    ERROR = "error"


# 「这一轮结束了」的消息种类。收到其中任何一条，界面都必须解锁。
# 集中列在这里是刻意的：v0.2.0 的检查流程漏了这件事，表现是检查跑完后
# 【开始安装】永远点不动——用户没有任何办法自己诊断出来。
TERMINAL_KINDS = frozenset(
    {Msg.CHECKED.value, Msg.DONE.value, Msg.STOPPED.value, Msg.ERROR.value}
)


class Worker(threading.Thread):
    """在后台跑一次「检查」或「安装」，通过队列把消息发回 UI。"""

    def __init__(self, q: queue.Queue, target: str, tryout: bool, do_install: bool,
                 *, jobs: int = 1, limit_rate: float = 0.0):
        super().__init__(daemon=True)
        self.q = q
        self.target = target
        self.tryout = tryout
        self.do_install = do_install
        self.jobs = jobs
        self.limit_rate = limit_rate
        self.stop_event = threading.Event()

    def say(self, kind: Msg, **payload) -> None:
        # 队列上放的仍是普通字符串（.value）：消费方不必知道枚举的存在。
        self.q.put((kind.value, payload))

    def run(self) -> None:
        try:
            with open_remote(jobs=self.jobs, limit_rate=self.limit_rate) as archive:
                self.say(Msg.STATUS, text="正在解析中央目录…", state="正在检查…")
                plan = self._plan(archive)
                self.say(Msg.PLAN, plan=plan)
                if not self.do_install:
                    self.say(Msg.CHECKED, plan=plan)
                    return
                if not plan.need:
                    self.say(Msg.DONE, result=Result(), plan=plan)
                    return
                self.say(Msg.STATUS, text="开始安装…", state="正在安装…")
                result = install(
                    archive,
                    plan,
                    on_progress=self._on_progress,
                    stop_event=self.stop_event,
                )
            self.say(Msg.STOPPED if result.stopped else Msg.DONE, result=result, plan=plan)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要让用户看见
            self.say(Msg.ERROR, text=f"{exc}\n\n{traceback.format_exc()}")

    def _plan(self, archive: Archive) -> Plan:
        excludes = config.EXCLUDE_STREAMING if self.tryout else ()
        return make_plan(archive, self.target, exclude_prefixes=excludes)

    def _on_progress(self, progress: Progress) -> None:
        self.say(Msg.PROGRESS, progress=progress)
