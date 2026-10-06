"""图形界面（Tkinter / 标准库自带，零依赖）。

这一层只负责「显示」与「转发」：规划、安装、续传判定全部在 :mod:`efd.planner`
和 :mod:`efd.installer` 里，CLI 与 GUI 走的是同一份代码。

线程模型：网络与磁盘操作在 worker 线程，UI 通过 ``queue`` + ``after()`` 轮询更新。
tkinter 不是线程安全的，**任何控件操作都不能在 worker 线程里做**。

界面按「从哪装 → 装什么 → 装到哪了」的顺序自上而下排：目录、动作、计划、进度、日志。
计划面板把「峰值磁盘占用」摆在最显眼的位置——那是这个工具存在的全部理由。

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

from . import __version__, config, detect, settings
from .archive import Archive, open_remote
from .installer import Progress, Result, install
from .planner import Plan, make_plan
from .util import free_bytes, human, human_time

APP_TITLE = "EF DOWNLOADER"
TAGLINE = "边下边解压 · 峰值磁盘占用约为官方方式的一半"

POLL_MS = 120
WINDOW = (980, 760)
MIN_WINDOW = (880, 640)

# 刻意**不再写死**成开发者自己的路径——那对别人毫无意义，只会让人怀疑
# 「这不是给我的东西」。默认值由 efd.settings 决定：上次用过的 → 注册表探测 → 留空。
DEFAULT_TARGET = ""

MUTED = "#666666"
GREEN = "#1a7f37"
ORANGE = "#b45309"
RED = "#b91c1c"


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
                self.say("status", text="正在解析中央目录…", state="正在检查…")
                plan = self._plan(archive)
                self.say("plan", plan=plan)
                if not self.do_install:
                    self.say("status", text="检查完成。", state="检查完成")
                    return
                if not plan.need:
                    self.say("done", result=Result(), plan=plan)
                    return
                self.say("status", text="开始安装…", state="正在安装…")
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
        self.minsize(*MIN_WINDOW)

        self.q: queue.Queue = queue.Queue()
        self.worker: Worker | None = None
        self.plan: Plan | None = None
        self._closing = False
        self._detail_shown = False

        # tkinter 回调里的异常默认会被静默吞掉，这里让它浮到日志窗。
        self.report_callback_exception = self._on_callback_error

        self._build()
        self._center()
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

    def _center(self) -> None:
        """把窗口摆到屏幕偏上的位置，别默认贴在左上角。"""
        width, height = WINDOW
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(width, max(MIN_WINDOW[0], screen_w - 80))
        height = min(height, max(MIN_WINDOW[1], screen_h - 120))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")

    # -------- 布局
    def _styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        style.configure("Head.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Tag.TLabel", foreground=MUTED)
        style.configure("Name.TLabel", foreground=MUTED)
        style.configure("Value.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Save.TLabel", foreground=GREEN,
                        font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Pct.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Stat.TLabel", font=("Consolas", 10))
        style.configure("Hint.TLabel", foreground=MUTED)
        style.configure("Action.TButton", padding=(16, 9))

    def _build(self) -> None:
        self._styles()

        head = ttk.Frame(self)
        head.pack(fill="x", padx=16, pady=(12, 0))
        ttk.Label(head, text=APP_TITLE, style="Head.TLabel").pack(side="left")
        ttk.Label(head, text=TAGLINE, style="Tag.TLabel").pack(side="left",
                                                               padx=(12, 0), pady=(6, 0))
        ttk.Label(head, text=f"v{__version__}", style="Tag.TLabel").pack(side="right",
                                                                        pady=(6, 0))

        self._build_target()
        self._build_actions()
        self._build_plan()
        self._build_progress()
        self._build_log()

        if self.var_target.get():
            self.log("就绪。已找到游戏目录，确认无误后点【1. 检查】。")
        else:
            self.log("就绪。没能自动找到游戏目录，请点【浏览…】手动选择安装位置。")

    def _build_target(self) -> None:
        box = ttk.LabelFrame(self, text="安装目录")
        box.pack(fill="x", padx=16, pady=(10, 4))

        row = ttk.Frame(box)
        row.pack(fill="x", padx=10, pady=(10, 6))
        self.var_target = tk.StringVar(value=settings.initial_target() or DEFAULT_TARGET)
        entry = ttk.Entry(row, textvariable=self.var_target)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _e: self.on_check())
        ttk.Button(row, text="浏览…", width=9, command=self._browse).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="自动检测", width=9, command=self._autodetect).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="打开目录", width=9, command=self._open_target).pack(side="left", padx=(6, 0))

        info = ttk.Frame(box)
        info.pack(fill="x", padx=10, pady=(0, 10))
        self.lbl_disk = ttk.Label(info, text="目标盘可用空间：—")
        self.lbl_disk.pack(side="left")
        self.lbl_need = ttk.Label(info, text="", style="Hint.TLabel")
        self.lbl_need.pack(side="right")

    def _build_actions(self) -> None:
        box = ttk.Frame(self)
        box.pack(fill="x", padx=16, pady=4)

        row = ttk.Frame(box)
        row.pack(fill="x")
        self.btn_check = ttk.Button(row, text="1. 检查", style="Action.TButton",
                                    command=self.on_check)
        self.btn_check.pack(side="left")
        self.btn_go = ttk.Button(row, text="2. 开始安装", style="Action.TButton",
                                 command=self.on_install, state="disabled")
        self.btn_go.pack(side="left", padx=(8, 0))
        self.btn_stop = ttk.Button(row, text="停止", style="Action.TButton",
                                   command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))

        self.var_tryout = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            box,
            text="快速试探：只装小文件、跳过 StreamingAssets（约 1.3 GB，先跑通流程用）",
            variable=self.var_tryout,
        ).pack(anchor="w", pady=(8, 0))

    def _build_plan(self) -> None:
        box = ttk.LabelFrame(self, text="计划")
        box.pack(fill="x", padx=16, pady=4)

        grid = ttk.Frame(box)
        grid.pack(fill="x", padx=10, pady=(10, 2))
        grid.columnconfigure(1, weight=1)

        def kv(row: int, name: str, style: str = "Value.TLabel"):
            ttk.Label(grid, text=name, style="Name.TLabel").grid(
                row=row, column=0, sticky="w", pady=1)
            value = ttk.Label(grid, text="—", style=style)
            value.grid(row=row, column=1, sticky="w", padx=(14, 0), pady=1)
            return value

        self.v_version = kv(0, "游戏版本")
        self.v_need = kv(1, "待安装")
        self.v_skip = kv(2, "已跳过")
        self.v_download = kv(3, "需要下载")
        ttk.Separator(grid, orient="horizontal").grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=5)
        self.v_peak = kv(5, "峰值磁盘")
        self.v_save = kv(6, "可省下", style="Save.TLabel")

        # 摆在第一行右侧，不占额外的纵向空间。
        self.btn_detail = ttk.Button(grid, text="详情 ▾", width=10, command=self._toggle_detail)
        self.btn_detail.grid(row=0, column=2, sticky="e")

        # 详情面板默认收起，但控件一直在——set_plan_text() 任何时候都能写进去。
        self.detail = ttk.Frame(box)
        self.txt_plan = tk.Text(self.detail, height=8, wrap="none", relief="flat",
                                background=self.cget("background"))
        self.txt_plan.pack(fill="x", padx=10, pady=(0, 8))
        self.txt_plan.configure(state="disabled")

    def _build_progress(self) -> None:
        box = ttk.LabelFrame(self, text="进度")
        box.pack(fill="x", padx=16, pady=4)

        row = ttk.Frame(box)
        row.pack(fill="x", padx=10, pady=(10, 4))
        self.bar = ttk.Progressbar(row, mode="determinate", maximum=1000)
        self.bar.pack(side="left", fill="x", expand=True)
        self.lbl_pct = ttk.Label(row, text="0.0%", style="Pct.TLabel", width=7, anchor="e")
        self.lbl_pct.pack(side="left", padx=(10, 0))

        self.lbl_stats = ttk.Label(box, text="—", style="Stat.TLabel")
        self.lbl_stats.pack(anchor="w", padx=10, pady=(0, 2))
        self.lbl_cur = ttk.Label(box, text="", style="Hint.TLabel")
        self.lbl_cur.pack(anchor="w", padx=10, pady=(0, 10))

    def _build_log(self) -> None:
        box = ttk.LabelFrame(self, text="日志")
        box.pack(fill="both", expand=True, padx=16, pady=(4, 14))

        bar = ttk.Frame(box)
        bar.pack(fill="x", padx=10, pady=(8, 0))
        self.lbl_state = ttk.Label(bar, text="就绪", style="Hint.TLabel")
        self.lbl_state.pack(side="left")
        ttk.Button(bar, text="清空", width=8, command=self._clear_log).pack(side="right")
        ttk.Button(bar, text="保存…", width=8, command=self._save_log).pack(side="right", padx=(0, 6))

        body = ttk.Frame(box)
        body.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        scroll = ttk.Scrollbar(body)
        scroll.pack(side="right", fill="y")
        self.txt_log = tk.Text(body, height=8, wrap="word", font=("Consolas", 9),
                               yscrollcommand=scroll.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        scroll.configure(command=self.txt_log.yview)

    # -------- 小工具
    def log(self, msg: str) -> None:
        self.txt_log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.txt_log.see("end")

    def set_state(self, text: str) -> None:
        self.lbl_state.configure(text=text)

    def set_plan_text(self, text: str) -> None:
        self.txt_plan.configure(state="normal")
        self.txt_plan.delete("1.0", "end")
        self.txt_plan.insert("1.0", text)
        self.txt_plan.configure(state="disabled")

    def _toggle_detail(self) -> None:
        self._detail_shown = not self._detail_shown
        if self._detail_shown:
            # grid 是先 pack 进去的，这里直接追加在它下面
            self.detail.pack(fill="x")
            self.btn_detail.configure(text="详情 ▴")
        else:
            self.detail.pack_forget()
            self.btn_detail.configure(text="详情 ▾")

    def _clear_log(self) -> None:
        self.txt_log.delete("1.0", "end")

    def _save_log(self) -> None:
        name = time.strftime("efd-%Y%m%d-%H%M%S.log")
        chosen = filedialog.asksaveasfilename(
            title="保存日志", defaultextension=".log", initialfile=name,
            filetypes=[("日志文件", "*.log"), ("所有文件", "*.*")],
        )
        if not chosen:
            return
        try:
            with open(chosen, "w", encoding="utf-8") as fh:
                fh.write(self.txt_log.get("1.0", "end"))
            self.log(f"日志已保存到 {chosen}")
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"保存失败：\n{exc}")

    def _refresh_disk(self) -> None:
        if self._closing:
            return
        target = self.var_target.get()
        if target:
            free = free_bytes(target)
            self.lbl_disk.configure(text=f"目标盘可用空间：{human(free)}")
        else:
            self.lbl_disk.configure(text="目标盘可用空间：—")
        self.after(3000, self._refresh_disk)

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(title="选择安装目录", initialdir=self.var_target.get())
        if chosen:
            self.var_target.set(os.path.normpath(chosen))
            self._refresh_disk()

    def _autodetect(self) -> None:
        found = detect.suggest_target()
        if found:
            self.var_target.set(found)
            self._refresh_disk()
            self.log(f"检测到游戏目录：{found}")
        else:
            self.log("没能自动找到游戏目录，请点【浏览…】手动选择。")

    def _open_target(self) -> None:
        path = self.var_target.get()
        if not path:
            messagebox.showinfo(APP_TITLE, "还没选择安装目录。")
            return
        if not os.path.isdir(path):
            messagebox.showwarning(APP_TITLE, f"目录不存在：\n{path}")
            return
        try:
            os.startfile(path)  # noqa: S606 - Windows 专用，正是本工具的目标平台
        except (AttributeError, OSError) as exc:
            self.log(f"打不开目录：{exc}")

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
        target = self.var_target.get()
        settings.remember_target(target)
        self.plan = None
        self._busy(True)
        self.worker = Worker(self.q, target, self.var_tryout.get(), do_install)
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
            self.log("已请求停止，等待当前文件写完…")
            self.set_state("正在停止…")
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
            if payload.get("state"):
                self.set_state(payload["state"])
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
        self.bar["value"] = 1000
        self.lbl_pct.configure(text="100.0%")
        if result.errors:
            self.set_state(f"完成，但有 {len(result.errors)} 个失败")
            self.log(f"⚠ 完成但有 {len(result.errors)} 个失败，重新点【开始安装】会重试。")
            for message in result.errors[:10]:
                self.log("   " + message)
        else:
            self.set_state("完成")
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
