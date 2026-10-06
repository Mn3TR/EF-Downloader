"""主窗口：把主题、面板、动作和消息泵组装成一个 ``tk.Tk``。

这个类本身很短——它的价值在于「谁拥有什么」一目了然：
窗口状态在这里，摆放控件在 :mod:`~efd.ui.panels`，
按钮行为在 :mod:`~efd.ui.handlers`。
"""

from __future__ import annotations

import os
import queue
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox

from ..core import detect
from ..core.planner import Plan
from ..core.util import free_bytes, human
from .handlers import HandlersMixin
from .messages import Worker
from .panels import PanelsMixin
from .theme import APP_TITLE, MIN_WINDOW, POLL_MS, WINDOW



class App(PanelsMixin, HandlersMixin, tk.Tk):
    """EF DOWNLOADER 的主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.minsize(*MIN_WINDOW)

        self.q: queue.Queue = queue.Queue()
        self.worker: Worker | None = None
        self.plan: Plan | None = None
        self._closing = False
        self._detail_shown = False
        self._busy_now = False

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
