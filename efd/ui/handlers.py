"""动作与消息泵：按钮点下去之后发生什么。

窗口是单线程的，worker 是另一个线程，两者之间只有 :class:`~efd.ui.messages.Msg`
消息这一条通道——所以「界面为什么卡住 / 为什么谎报完成」这类问题，
答案永远在这个文件的 :meth:`HandlersMixin._handle` 里。
"""

from __future__ import annotations

import queue
import traceback
from tkinter import messagebox

from ..core import settings
from ..core.planner import Plan
from ..core.throttle import RateError, check_jobs, format_rate, parse_rate
from ..core.util import free_bytes, human, human_time
from .messages import TERMINAL_KINDS, Msg, Worker
from .theme import APP_TITLE, POLL_MS


class HandlersMixin:
    """按钮行为、定时泵与收尾汇报。

    ``_handle`` 是唯一的消息消费点：worker 说什么，界面才动什么。
    """

    def _busy(self, on: bool) -> None:
        self._busy_now = on
        self.btn_check.configure(state="disabled" if on else "normal")
        self.btn_go.configure(
            state="disabled" if on else ("normal" if self.plan and self.plan.need else "disabled")
        )
        self.btn_stop.configure(state="normal" if on else "disabled")

    def _net(self) -> tuple[int, float] | None:
        """解析并发与限速。填错了当场弹窗，不要等到下载中途才炸。"""
        try:
            jobs = check_jobs(self.var_jobs.get())
            rate = parse_rate(self.var_rate.get())
        except RateError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return None
        return jobs, rate

    def _start(self, do_install: bool) -> None:
        if self.worker and self.worker.is_alive():
            return
        net = self._net()
        if net is None:
            return
        jobs, rate = net
        target = self.var_target.get()
        settings.remember_target(target)
        settings.remember_net(self.var_jobs.get(), self.var_rate.get())
        self.plan = None
        self._busy(True)
        self.log(f"网络：并发预读 {jobs} 线程，限速 {format_rate(rate)}。")
        self.worker = Worker(self.q, target, self.var_tryout.get(), do_install,
                             jobs=jobs, limit_rate=rate)
        self.worker.start()

    def on_check(self) -> None:
        if not self.var_target.get():
            messagebox.showwarning(APP_TITLE, "请先选择安装目录。")
            return
        self.log("开始检查清单…")
        self.set_state("正在检查…")
        self._start(do_install=False)

    def on_install(self) -> None:
        if not self.plan or not self.plan.need:
            return
        free = free_bytes(self.var_target.get())
        required = self.plan.need_uncompressed + self.plan.biggest
        if free and free < required:
            if not messagebox.askyesno(
                "空间可能不足",
                f"目标盘可用 {human(free)}，本次需要约 {human(required)}。\n\n仍要继续吗？",
            ):
                return
        self.log("开始安装…")
        self.set_state("正在安装…")
        self._start(do_install=True)

    def on_stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.stop_event.set()
            self.log("已请求停止，当前文件会中断并丢弃（下次从头下）。")
            self.set_state("正在停止…")
            self.btn_stop.configure(state="disabled")

    def _watchdog(self) -> None:
        """兜底：worker 已经结束了，界面却还锁着，就解锁。

        正常路径由 :data:`TERMINAL_KINDS` 负责。这条线是防止**将来**新增的
        退出分支又忘了发结束消息——那种故障的表现是「按钮点不动」，
        用户既看不懂也绕不过去，只能重启程序。
        """
        if self._busy_now and self.worker is not None and not self.worker.is_alive():
            self.log("（后台任务已结束，恢复按钮）")
            self._busy(False)

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                try:
                    self._handle(kind, payload)
                except Exception:  # noqa: BLE001
                    self.log("⚠ UI 处理消息出错：\n" + traceback.format_exc())
        except queue.Empty:
            pass
        finally:
            # 先把队列抽干再看 worker 死活：它的消息一定在它退出之前就发出去了，
            # 所以「队列空 + 线程死」才等于这一轮真的处理完。
            self._watchdog()
            # 无论如何都要续上，否则泵一死界面就再也不更新了。
            if not self._closing:
                self.after(POLL_MS, self._poll)

    def _handle(self, kind: str, payload: dict) -> None:
        if kind in TERMINAL_KINDS:
            # 集中解锁：不管这一轮是怎么结束的，按钮都得还原。
            # 「计划已到」不代表结束——检查流程还要再发一条 checked。
            self._busy(False)
        if kind == Msg.STATUS:
            self.log(payload["text"])
            if payload.get("state"):
                self.set_state(payload["state"])
        elif kind == Msg.CHECKED:
            self.log("检查完成。")
        elif kind == Msg.PLAN:
            self._show_plan(payload["plan"])
        elif kind == Msg.PROGRESS:
            self._show_progress(payload["progress"])
        elif kind == Msg.DONE:
            self._finish(payload["result"], payload["plan"], stopped=False)
        elif kind == Msg.STOPPED:
            self._finish(payload["result"], payload["plan"], stopped=True)
        elif kind == Msg.ERROR:
            self.set_state("出错")
            self.log("❌ 出错：\n" + payload["text"])
            messagebox.showerror("出错", payload["text"][:1500])

    def _show_plan(self, plan: Plan) -> None:
        self.plan = plan
        self.v_version.configure(text=plan.version)
        self.v_need.configure(
            text=f"{plan.need_count} 个文件 · {human(plan.need_uncompressed)}")
        self.v_skip.configure(
            text=f"{plan.already_count} 个文件 · {human(plan.already_bytes)}")
        self.v_download.configure(text=human(plan.need_compressed))
        self.v_peak.configure(
            text=f"本工具 {human(plan.peak_ours)}　　官方方式 {human(plan.peak_official)}")
        self.v_save.configure(text=f"{human(plan.saving)}")

        required = plan.need_uncompressed + plan.biggest
        self.lbl_need.configure(text=f"本次需要约 {human(required)}")

        self.set_plan_text(
            f"清单条目        {plan.entries_total}\n"
            f"待安装          {plan.need_count} 个   {human(plan.need_uncompressed)}"
            f"  (下载 {human(plan.need_compressed)})\n"
            f"已存在跳过      {plan.already_count} 个   {human(plan.already_bytes)}\n"
            f"排除            {len(plan.excluded)} 个\n"
            f"最大单文件      {human(plan.biggest)}   ← 缓冲/续传粒度\n"
            f"清单下载成本    {human(plan.manifest_bytes)}\n"
            f"{'─' * 58}\n"
            f"峰值磁盘  本方案 {human(plan.peak_ours)}     "
            f"官方方式 {human(plan.peak_official)}"
        )

        if plan.need:
            self.lbl_stats.configure(
                text=f"就绪：待装 {plan.need_count} 个文件 / {human(plan.need_uncompressed)}")
            self.set_state("检查完成，可以开始安装")
        else:
            self.lbl_stats.configure(text="就绪：没有需要安装的文件")
            self.set_state("没有需要安装的文件")
        self.bar["value"] = 0
        self.lbl_pct.configure(text="0.0%")

    def _show_progress(self, p: Progress) -> None:
        pct = max(0.0, min(1.0, p.fraction))
        self.bar["value"] = int(pct * 1000)
        self.lbl_pct.configure(text=f"{pct * 100:.1f}%")
        self.lbl_stats.configure(
            text=(f"{p.done}/{p.total} 个文件   {human(p.written)} / {human(p.total_uncompressed)}"
                  f"   {human(p.speed)}/s   剩余 {human_time(p.eta)}")
        )
        self.lbl_cur.configure(text=p.current[-96:])

    def _finish(self, result: Result, plan: Plan, *, stopped: bool) -> None:
        self._busy(False)
        self._refresh_disk()
        if stopped:
            self.set_state("已停止")
            self.log(f"⏹ 已停止（本次写出 {human(result.written)}，{result.done} 个文件）。"
                     f"重新点【开始安装】会自动续传。")
            return
        # 出错时 install() 会在第一个失败处提前返回（GUI 从不传 keep_going），
        # 剩下的文件根本没碰。这种情况下画到 100% 并弹「安装完成」就是在撒谎——
        # 用户会以为装完了，实际上一个大文件都没装成。
        if result.errors:
            total = max(1, plan.need_count)
            pct = max(0.0, min(1.0, result.done / total))
            self.bar["value"] = int(pct * 1000)
            self.lbl_pct.configure(text=f"{pct * 100:.1f}%")
            self.set_state(f"中断，{len(result.errors)} 个错误")
            self.log(f"⚠ 安装中断：{result.done}/{plan.need_count} 个文件已完成，"
                     f"之后没有再继续（本次共 {len(result.errors)} 个错误）。")
            for message in result.errors[:10]:
                self.log("   " + message)
            self.lbl_stats.configure(
                text=f"中断：{result.done}/{plan.need_count} 个文件 / {human(result.written)}")
            messagebox.showwarning(
                "未完成",
                f"安装中断，还有 {plan.need_count - result.done} 个文件没装。\n\n"
                f"错误 {len(result.errors)} 个（首个：{result.errors[0][:200]}）\n\n"
                f"已完成 {result.done} 个文件 / 写出 {human(result.written)}\n"
                f"下载 {human(result.net_bytes)}\n耗时 {human_time(result.elapsed)}\n\n"
                f"重新点【开始安装】会接着装，已装好的会跳过。",
            )
            return
        self.bar["value"] = 1000
        self.lbl_pct.configure(text="100.0%")
        self.set_state("完成")
        self.log(f"✅ 完成：{result.done} 个文件，写出 {human(result.written)}，"
                 f"下载 {human(result.net_bytes)}，耗时 {human_time(result.elapsed)}")
        self.lbl_stats.configure(text=f"完成：{result.done} 个文件 / {human(result.written)}")
        messagebox.showinfo(
            "完成",
            f"安装完成。\n\n写出 {human(result.written)}\n"
            f"下载 {human(result.net_bytes)}\n耗时 {human_time(result.elapsed)}",
        )
