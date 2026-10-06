"""图形界面（Tkinter / 标准库自带，零依赖）。

这一层只负责「显示」与「转发」：规划、安装、续传判定全部在 :mod:`efd.planner`
和 :mod:`efd.installer` 里，CLI 与 GUI 走的是同一份代码。

线程模型：网络与磁盘操作在 worker 线程，UI 通过 ``queue`` + ``after()`` 轮询更新。
tkinter 不是线程安全的，**任何控件操作都不能在 worker 线程里做**。

启动：``python -m efd gui``
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk

from . import config, detect
from .archive import Archive, open_remote
from .installer import Progress, Result, install
from .planner import Plan, make_plan
from .util import free_bytes, human, human_time

APP_TITLE = "终末地 · 省空间安装器"
POLL_MS = 120

# 刻意**不再写死**成开发者自己的路径——那对别人毫无意义，只会让人怀疑
# 「这不是给我的东西」。默认值由 efd.detect 从注册表里推断；推断不出来
# 就留空，界面上提示用户自己选。
DEFAULT_TARGET = ""


class Worker(threading.Thread):
    """在后台跑一次「检查」或「安装」，通过队列把消息发回 UI。"""

    def __init__(self, q: queue.Queue, target: str, tryout: bool, do_install: bool):
        super().__init__(daemon=True)
        self.q = q
        self.target = target
        self.tryout = tryout
        self.do_install = do_install
        self.stop_event = threading.Event()

    def say(self, kind: str, **payload) -> None:
        self.q.put((kind, payload))

    def run(self) -> None:
        try:
            with open_remote() as archive:
                self.say("status", text="正在解析中央目录…")
                plan = self._plan(archive)
                self.say("plan", plan=plan)
                if not self.do_install:
                    self.say("status", text="检查完成")
                    return
                if not plan.need:
                    self.say("done", result=Result(), plan=plan)
                    return
                self.say("status", text="开始安装…")
                result = install(
                    archive,
                    plan,
                    on_progress=self._on_progress,
                    stop_event=self.stop_event,
                )
            self.say("stopped" if result.stopped else "done", result=result, plan=plan)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要让用户看见
            self.say("error", text=f"{exc}\n\n{traceback.format_exc()}")

    def _plan(self, archive: Archive) -> Plan:
        excludes = config.EXCLUDE_STREAMING if self.tryout else ()
        return make_plan(archive, self.target, exclude_prefixes=excludes)

    def _on_progress(self, progress: Progress) -> None:
        self.say("progress", progress=progress)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("920x680")
        self.minsize(820, 600)

        self.q: queue.Queue = queue.Queue()
        self.worker: Worker | None = None
        self.plan: Plan | None = None
        self._closing = False

        # tkinter 回调里的异常默认会被静默吞掉，这里让它浮到日志窗。
        self.report_callback_exception = self._on_callback_error

        self._build()
        self.after(POLL_MS, self._poll)
        self._refresh_disk()

    def _on_callback_error(self, exc, val, tb) -> None:
        try:
            self.log("⚠ 回调异常：\n" + "".join(traceback.format_exception(exc, val, tb)))
        except Exception:  # noqa: BLE001
            traceback.print_exception(exc, val, tb)

    def destroy(self) -> None:
        """关窗前取消挂起的定时回调。

        否则 Tk 会在解释器销毁后继续触发它们，并打出一串
        ``while executing "..." ("after" script)`` 后台错误。
        """
        self._closing = True
        try:
            for after_id in self.tk.call("after", "info"):
                try:
                    self.after_cancel(after_id)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        super().destroy()

    # -------- 布局
    def _build(self) -> None:
        pad = dict(padx=10, pady=6)
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        self.option_add("*Font", ("Microsoft YaHei UI", 10))

        top = ttk.LabelFrame(self, text="安装目录")
        top.pack(fill="x", **pad)
        self.var_target = tk.StringVar(value=detect.suggest_target() or DEFAULT_TARGET)
        row = ttk.Frame(top)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Entry(row, textvariable=self.var_target).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…", command=self._browse, width=8).pack(side="left", padx=(6, 0))
        self.lbl_disk = ttk.Label(top, text="磁盘：—")
        self.lbl_disk.pack(anchor="w", padx=10, pady=(0, 8))

        opt = ttk.Frame(self)
        opt.pack(fill="x", **pad)
        self.var_tryout = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="只装小文件（跳过 StreamingAssets，约 1.3 GB，用于试探）",
                        variable=self.var_tryout).pack(side="left")
        self.btn_check = ttk.Button(opt, text="1. 检查", command=self.on_check, width=12)
        self.btn_check.pack(side="right")
        self.btn_go = ttk.Button(opt, text="2. 开始安装", command=self.on_install,
                                 width=14, state="disabled")
        self.btn_go.pack(side="right", padx=(0, 8))
        self.btn_stop = ttk.Button(opt, text="停止", command=self.on_stop,
                                   width=8, state="disabled")
        self.btn_stop.pack(side="right", padx=(0, 8))

        box = ttk.LabelFrame(self, text="计划")
        box.pack(fill="x", **pad)
        self.txt_plan = tk.Text(box, height=7, wrap="none", relief="flat",
                                background=self.cget("background"))
        self.txt_plan.pack(fill="x", padx=8, pady=6)
        self.txt_plan.configure(state="disabled")

        prog = ttk.LabelFrame(self, text="进度")
        prog.pack(fill="x", **pad)
        self.lbl_stats = ttk.Label(prog, text="—", font=("Consolas", 10))
        self.lbl_stats.pack(anchor="w", padx=10, pady=(8, 2))
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=1000)
        self.bar.pack(fill="x", padx=10, pady=4)
        self.lbl_cur = ttk.Label(prog, text="", foreground="#666")
        self.lbl_cur.pack(anchor="w", padx=10, pady=(0, 8))

        logf = ttk.LabelFrame(self, text="日志")
        logf.pack(fill="both", expand=True, **pad)
        self.txt_log = tk.Text(logf, height=10, wrap="word", font=("Consolas", 9))
        scroll = ttk.Scrollbar(logf, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt_log.pack(fill="both", expand=True, padx=(8, 0), pady=6)

        if self.var_target.get():
            self.log(f"{APP_TITLE} 就绪。已自动找到游戏目录，确认后点【1. 检查】。")
        else:
            self.log(f"{APP_TITLE} 就绪。没能自动找到游戏目录，请先点【浏览…】选择安装位置。")

    # -------- 小工具
    def log(self, msg: str) -> None:
        self.txt_log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.txt_log.see("end")

    def set_plan_text(self, text: str) -> None:
        self.txt_plan.configure(state="normal")
        self.txt_plan.delete("1.0", "end")
        self.txt_plan.insert("1.0", text)
        self.txt_plan.configure(state="disabled")

    def _refresh_disk(self) -> None:
        if self._closing:
            return
        free = free_bytes(self.var_target.get())
        self.lbl_disk.configure(text=f"目标盘可用空间：{human(free)}")
        self.after(3000, self._refresh_disk)

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(title="选择安装目录", initialdir=self.var_target.get())
        if chosen:
            self.var_target.set(os.path.normpath(chosen))
            self._refresh_disk()

    # -------- 动作
    def _busy(self, on: bool) -> None:
        self.btn_check.configure(state="disabled" if on else "normal")
        self.btn_go.configure(
            state="disabled" if on else ("normal" if self.plan and self.plan.need else "disabled")
        )
        self.btn_stop.configure(state="normal" if on else "disabled")

    def _start(self, do_install: bool) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.plan = None
        self._busy(True)
        self.worker = Worker(self.q, self.var_target.get(), self.var_tryout.get(), do_install)
        self.worker.start()

    def on_check(self) -> None:
        self.log("开始检查清单…")
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
        self._start(do_install=True)

    def on_stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.stop_event.set()
            self.log("已请求停止，等待当前文件写完…")
            self.btn_stop.configure(state="disabled")

    # -------- 消息泵
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
            # 无论如何都要续上，否则泵一死界面就再也不更新了。
            if not self._closing:
                self.after(POLL_MS, self._poll)

    def _handle(self, kind: str, payload: dict) -> None:
        if kind == "status":
            self.log(payload["text"])
        elif kind == "plan":
            self._show_plan(payload["plan"])
        elif kind == "progress":
            self._show_progress(payload["progress"])
        elif kind == "done":
            self._finish(payload["result"], payload["plan"], stopped=False)
        elif kind == "stopped":
            self._finish(payload["result"], payload["plan"], stopped=True)
        elif kind == "error":
            self._busy(False)
            self.log("❌ 出错：\n" + payload["text"])
            messagebox.showerror("出错", payload["text"][:1500])

    def _show_plan(self, plan: Plan) -> None:
        self.plan = plan
        self.set_plan_text(
            f"游戏版本        {plan.version}\n"
            f"清单条目        {plan.entries_total}\n"
            f"已存在跳过      {plan.already_count} 个   {human(plan.already_bytes)}\n"
            f"待安装          {plan.need_count} 个   {human(plan.need_uncompressed)}"
            f"  (下载 {human(plan.need_compressed)})\n"
            f"排除            {len(plan.excluded)} 个\n"
            f"最大单文件      {human(plan.biggest)}   ← 缓冲/续传粒度\n"
            f"清单下载成本    {human(plan.manifest_bytes)}\n"
            f"{'─' * 58}\n"
            f"峰值磁盘  本方案 {human(plan.peak_ours)}     "
            f"官方方式 {human(plan.peak_official)}"
        )
        if plan.need:
            self.lbl_stats.configure(
                text=f"就绪：待装 {plan.need_count} 个文件 / {human(plan.need_uncompressed)}"
            )
        else:
            self.lbl_stats.configure(text="就绪：没有需要安装的文件")
        self.bar["value"] = 0

    def _show_progress(self, p: Progress) -> None:
        self.bar["value"] = int(p.fraction * 1000)
        self.lbl_stats.configure(
            text=(f"{p.done}/{p.total} 个文件   {human(p.written)} / {human(p.total_uncompressed)}"
                  f"   {human(p.speed)}/s   剩余 {human_time(p.eta)}")
        )
        self.lbl_cur.configure(text=p.current[-96:])

    def _finish(self, result: Result, plan: Plan, *, stopped: bool) -> None:
        self._busy(False)
        self._refresh_disk()
        if stopped:
            self.log(f"⏹ 已停止（本次写出 {human(result.written)}，{result.done} 个文件）。"
                     f"重新点【开始安装】会自动续传。")
            return
        self.bar["value"] = 1000
        if result.errors:
            self.log(f"⚠ 完成但有 {len(result.errors)} 个失败，重新点【开始安装】会重试。")
            for message in result.errors[:10]:
                self.log("   " + message)
        self.log(f"✅ 完成：{result.done} 个文件，写出 {human(result.written)}，"
                 f"下载 {human(result.net_bytes)}，耗时 {human_time(result.elapsed)}")
        self.lbl_stats.configure(text=f"完成：{result.done} 个文件 / {human(result.written)}")
        messagebox.showinfo(
            "完成",
            f"安装完成。\n\n写出 {human(result.written)}\n"
            f"下载 {human(result.net_bytes)}\n耗时 {human_time(result.elapsed)}",
        )


def detach_console() -> None:
    """冻结成 exe 后，把控制台窗口从 GUI 进程上摘掉。

    源码运行时什么都不做。这样**同一个 exe** 既能双击出图形界面，
    也能在终端里跑 ``efd plan`` 这类命令——不需要打包两个可执行文件。
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        ctypes.windll.kernel32.FreeConsole()
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    detach_console()
    try:  # 高 DPI 屏下不糊
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    App().mainloop()


__all__ = ["APP_TITLE", "DEFAULT_TARGET", "App", "Worker", "detach_console", "main"]
