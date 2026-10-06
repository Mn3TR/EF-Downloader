"""按钮行为、消息泵与窗口杂项。

**过去的三个 bug 全都住在这个层里**，而覆盖率数据说这里最薄弱
（``efd/ui/handlers.py`` 61.6%、``efd/ui/app.py`` 68.2%）：

- v0.2.1：【开始安装】永远点不动（检查流程没发结束消息）；
- v0.2.2：【停止】点了没反应（停机只在文件之间检查）；
- v0.2.3：大文件下载中断却报"安装完成"。

共同点是**它们都只在真实点按流程里才出现**，纯数据层的测试看不见。
所以这里刻意不 mock 界面本身：窗口真建出来，按的是真的回调。

没有显示环境时整组跳过。
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.test_gui import _REASON, state


def fake_plan(target: str, entries=()) -> "Plan":
    """造一个只够界面用的计划。

    真实 ``Plan`` 有 5 个必填字段；这里全填 0——界面这一层只看
    ``need``/``need_count``/``biggest`` 这几个派生属性，别的字段碰不到。
    """
    from efd.core.planner import Plan

    return Plan(version="1.5.3", target=target, archive_bytes=0,
                manifest_bytes=0, entries_total=len(entries), need=list(entries))


def fake_entry(size: int, compressed: int = 0, name: str = "a", offset: int = 0):
    return mock.Mock(size=size, compressed=compressed, name=name, offset=offset)


@unittest.skipIf(_REASON is not None, _REASON or "")
class _AppCase(unittest.TestCase):
    """建一个真窗口，并把它的弹窗全部收进列表里。"""

    def setUp(self):
        from efd.ui import App
        from efd.ui import app as app_module
        from efd.ui import handlers

        self.app = App()
        self.addCleanup(self.app.destroy)

        self.info: list[tuple] = []
        self.warn: list[tuple] = []
        self.ask: list[tuple] = []
        self.ask_answer = True

        def answer(*a, **k):
            self.ask.append(a)
            return self.ask_answer

        for module, name, sink in (
            (handlers.messagebox, "showinfo", self.info),
            (handlers.messagebox, "showwarning", self.warn),
            (handlers.messagebox, "showerror", self.warn),
            (app_module.messagebox, "showinfo", self.info),
            (app_module.messagebox, "showwarning", self.warn),
            (app_module.messagebox, "showerror", self.warn),
        ):
            p = mock.patch.object(module, name,
                                  side_effect=lambda *a, _s=sink, **k: _s.append(a))
            p.start()
            self.addCleanup(p.stop)

        for module in (handlers.messagebox, app_module.messagebox):
            p = mock.patch.object(module, "askyesno", side_effect=answer)
            p.start()
            self.addCleanup(p.stop)

    def target_dir(self) -> str:
        path = tempfile.mkdtemp(prefix="efd_ui_")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path


@unittest.skipIf(_REASON is not None, _REASON or "")
class TestStartGuards(_AppCase):
    """按【1. 检查】之前必须先把能拦的错拦下来。"""

    def test_check_without_a_directory_warns_and_does_not_start(self):
        self.app.var_target.set("")
        self.app.on_check()
        self.assertEqual(len(self.warn), 1, "空目录没有拦下来")
        self.assertIsNone(self.app.worker)

    def test_a_bad_jobs_value_warns_before_any_thread_starts(self):
        """并发填错了要当场说，不能等下载到一半才炸。"""
        self.app.var_target.set(self.target_dir())
        self.app.var_jobs.set("999")
        self.app.on_check()
        self.assertTrue(self.warn, "非法并发没有提示")
        self.assertIsNone(self.app.worker)

    def test_a_bad_rate_value_warns_before_any_thread_starts(self):
        self.app.var_target.set(self.target_dir())
        self.app.var_rate.set("快一点")
        self.app.on_check()
        self.assertTrue(self.warn, "非法限速没有提示")
        self.assertIsNone(self.app.worker)

    def test_install_without_a_plan_does_nothing(self):
        self.app.plan = None
        self.app.on_install()
        self.assertIsNone(self.app.worker)

    def test_install_with_an_empty_plan_does_nothing(self):
        self.app.plan = fake_plan(self.target_dir())
        self.app.on_install()
        self.assertIsNone(self.app.worker)


@unittest.skipIf(_REASON is not None, _REASON or "")
class TestStartActuallyStarts(_AppCase):
    """真的点下去，worker 要起得来，且它拿到的是界面上的值。"""

    def setUp(self):
        super().setUp()
        self.started: list[dict] = []

        class FakeWorker:
            def __init__(self, q, target, tryout, do_install, *, jobs, limit_rate):
                self.q, self.target, self.tryout = q, target, tryout
                self.do_install, self.jobs, self.limit_rate = do_install, jobs, limit_rate
                self.stop_event = threading.Event()
                self.started = False
                self.alive = False
                self.spec = dict(target=target, tryout=tryout, do_install=do_install,
                                 jobs=jobs, limit_rate=limit_rate)
                self.started = True
                self.alive = True
                started.append(self.spec)

            def is_alive(self):
                return self.alive

            def start(self):
                pass

        started = self.started
        self.FakeWorker = FakeWorker

    def _patch_worker(self):
        from efd.ui import handlers

        p = mock.patch.object(handlers, "Worker", self.FakeWorker)
        p.start()
        self.addCleanup(p.stop)

    def test_check_starts_a_worker_with_the_ui_settings(self):
        self._patch_worker()
        target = self.target_dir()
        self.app.var_target.set(target)
        self.app.var_jobs.set("4")
        self.app.var_rate.set("2M")
        self.app.var_tryout.set(True)

        self.app.on_check()

        self.assertEqual(len(self.started), 1)
        spec = self.started[0]
        self.assertEqual(spec["target"], target)
        self.assertFalse(spec["do_install"], "检查却当成了安装")
        self.assertTrue(spec["tryout"], "快速试探没有被带上")
        self.assertEqual(spec["jobs"], 4)
        self.assertEqual(spec["limit_rate"], 2 * 1024 * 1024)

    def test_check_locks_the_buttons_and_logs_the_network(self):
        self._patch_worker()
        self.app.var_target.set(self.target_dir())
        self.app.on_check()

        self.assertEqual(state(self.app.btn_check), "disabled")
        self.assertEqual(state(self.app.btn_stop), "normal")
        self.assertIn("检查", self.app.lbl_state.cget("text"))
        self.assertIn("预读", self.app.txt_log.get("1.0", "end"))

    def test_start_is_a_no_op_while_a_worker_is_running(self):
        """连点两下不该起两个 worker——那会往同一个目录里写两遍。"""
        self._patch_worker()
        self.app.var_target.set(self.target_dir())
        self.app.on_check()
        self.app.on_check()
        self.assertEqual(len(self.started), 1, "重复点击起了第二个 worker")

    def test_settings_are_remembered_for_next_launch(self):
        from efd.core import settings

        self._patch_worker()
        self.app.var_target.set(self.target_dir())
        self.app.var_jobs.set("3")
        self.app.var_rate.set("0")

        self.app.on_check()

        self.assertEqual(settings.remembered_target(), self.app.var_target.get())
        self.assertEqual(settings.remembered_net(), ("3", "0"))

    def test_install_warns_when_the_disk_is_too_small(self):
        """空间不够要问一句：这是"白下几十 G"和"提前退出"之间的区别。"""
        self._patch_worker()
        self.app.plan = fake_plan(self.target_dir(),
                                  [fake_entry(size=10**13, compressed=10**13)])
        self.app.var_target.set(self.app.plan.target)
        self.ask_answer = False
        self.app.on_install()
        self.assertEqual(len(self.ask), 1, "空间不足却没有征求确认")
        self.assertEqual(self.started, [], "用户说了不要，还是开工了")

    def test_install_proceeds_when_the_user_says_yes(self):
        self._patch_worker()
        self.app.plan = fake_plan(self.target_dir(),
                                  [fake_entry(size=10**13, compressed=10**13)])
        self.app.var_target.set(self.app.plan.target)
        self.app.on_install()
        self.assertEqual(len(self.started), 1)

    def test_install_does_not_ask_when_there_is_room(self):
        self._patch_worker()
        self.app.plan = fake_plan(self.target_dir(),
                                  [fake_entry(size=1024, compressed=512)])
        self.app.var_target.set(self.app.plan.target)
        self.app.on_install()
        self.assertEqual(self.ask, [], "空间充足却弹了确认框")
        self.assertEqual(len(self.started), 1)
        self.assertTrue(self.started[0]["do_install"])


@unittest.skipIf(_REASON is not None, _REASON or "")
class TestStop(_AppCase):
    def test_stop_sets_the_event_and_disables_itself(self):
        from efd.ui import handlers

        class Live:
            def __init__(self):
                self.stop_event = threading.Event()

            def is_alive(self):
                return True

        worker = Live()
        self.app.worker = worker
        self.app.on_stop()

        self.assertTrue(worker.stop_event.is_set(), "停机信号没有发出去")
        self.assertEqual(state(self.app.btn_stop), "disabled")
        self.assertIn("停止", self.app.lbl_state.cget("text"))
        self.assertIn("中断", self.app.txt_log.get("1.0", "end"),
                      "没有告诉用户当前文件会被丢弃")

    def test_stop_without_a_worker_is_harmless(self):
        self.app.worker = None
        self.app.on_stop()
        self.assertEqual(state(self.app.btn_stop), "disabled")


@unittest.skipIf(_REASON is not None, _REASON or "")
class TestMessagePump(_AppCase):
    """``_poll`` 是唯一把队列里的消息变成界面变化的地方。"""

    def test_a_status_message_lands_in_the_log(self):
        self.app._handle("status", {"text": "正在解析中央目录…", "state": "正在检查…"})
        self.assertIn("解析中央目录", self.app.txt_log.get("1.0", "end"))
        self.assertIn("正在检查", self.app.lbl_state.cget("text"))

    def test_an_unknown_kind_does_not_crash_the_pump(self):
        """将来加了新消息种类，旧版本不该白屏。"""
        self.app._handle("something_new", {})
        self.assertEqual(state(self.app.btn_check), "normal")

    def test_a_broken_handler_is_caught_by_the_pump(self):
        """单条消息处理失败不能让泵停摆——否则界面永久冻结。"""
        self.app.q.put(("plan", {"plan": None}))  # _show_plan 会炸
        self.app.q.put(("status", {"text": "还在跑"}))

        self.app._poll()

        text = self.app.txt_log.get("1.0", "end")
        self.assertIn("UI 处理消息出错", text, "异常被静默吞了")
        self.assertIn("还在跑", text, "一条坏消息后面的消息也没了")

    def test_poll_drains_the_whole_queue(self):
        for i in range(5):
            self.app.q.put(("status", {"text": f"消息{i}"}))
        self.app._poll()
        text = self.app.txt_log.get("1.0", "end")
        for i in range(5):
            self.assertIn(f"消息{i}", text)

    def test_poll_stops_rescheduling_once_closing(self):
        """关窗后不能再挂定时回调，否则 Tk 会打出后台错误。"""
        self.app._closing = False
        self.app._poll()
        pending = len(self.app.tk.call("after", "info"))
        self.app._closing = True
        self.app._poll()
        self.assertLessEqual(len(self.app.tk.call("after", "info")), pending)

    def test_terminal_messages_unlock_the_buttons(self):
        from efd.core.installer import Result

        plan = fake_plan(self.target_dir())
        for kind in ("checked", "done", "stopped", "error"):
            with self.subTest(kind=kind):
                self.app._busy(True)
                payload = {"plan": plan, "result": Result(), "text": "x"}
                if kind == "error":
                    payload = {"text": "x"}
                self.app._handle(kind, payload)
                self.assertEqual(state(self.app.btn_check), "normal",
                                 f"{kind} 之后按钮没有解锁")


@unittest.skipIf(_REASON is not None, _REASON or "")
class TestWindowExtras(_AppCase):
    def test_the_target_field_is_trimmed_into_a_normal_path(self):
        d = self.target_dir()
        p = mock.patch("efd.ui.app.filedialog.askdirectory", return_value=d)
        p.start()
        self.addCleanup(p.stop)
        self.app._browse()
        self.assertEqual(self.app.var_target.get(), str(Path(d)))

    def test_cancelling_the_browser_keeps_the_old_value(self):
        self.app.var_target.set("D:/keepme")
        p = mock.patch("efd.ui.app.filedialog.askdirectory", return_value="")
        p.start()
        self.addCleanup(p.stop)
        self.app._browse()
        self.assertEqual(self.app.var_target.get(), "D:/keepme")

    def test_autodetect_fills_the_field_when_it_finds_something(self):
        from efd.ui import app as app_module

        found = self.target_dir()
        p = mock.patch.object(app_module.detect, "suggest_target", return_value=found)
        p.start()
        self.addCleanup(p.stop)
        self.app._autodetect()
        self.assertEqual(self.app.var_target.get(), found)
        self.assertIn("检测到", self.app.txt_log.get("1.0", "end"))

    def test_autodetect_tells_the_user_what_to_do_when_it_fails(self):
        from efd.ui import app as app_module

        p = mock.patch.object(app_module.detect, "suggest_target", return_value="")
        p.start()
        self.addCleanup(p.stop)
        self.app._autodetect()
        self.assertIn("浏览", self.app.txt_log.get("1.0", "end"))

    def test_open_target_on_an_empty_field_explains_itself(self):
        self.app.var_target.set("")
        self.app._open_target()
        self.assertTrue(self.info, "空目录没有任何提示")

    def test_open_target_warns_about_a_missing_directory(self):
        self.app.var_target.set("D:/definitely/not/here")
        self.app._open_target()
        self.assertTrue(self.warn, "目录不存在却没有提示")

    def test_disk_label_says_unknown_for_an_empty_target(self):
        self.app.var_target.set("")
        self.app._refresh_disk()
        self.assertIn("—", self.app.lbl_disk.cget("text"))

    def test_disk_label_shows_a_real_number_for_a_real_directory(self):
        from efd.core.util import human, free_bytes

        d = self.target_dir()
        self.app.var_target.set(d)
        self.app._refresh_disk()
        self.assertIn(human(free_bytes(d)), self.app.lbl_disk.cget("text"))

    def test_saving_the_log_writes_what_is_on_screen(self):
        chosen = str(Path(self.target_dir()) / "out.log")
        self.app.log("要保存的内容")
        p = mock.patch("efd.ui.app.filedialog.asksaveasfilename", return_value=chosen)
        p.start()
        self.addCleanup(p.stop)
        self.app._save_log()
        self.assertIn("要保存的内容", Path(chosen).read_text(encoding="utf-8"))

    def test_cancelling_the_save_dialog_writes_nothing(self):
        self.app.log("不该落盘")
        p = mock.patch("efd.ui.app.filedialog.asksaveasfilename", return_value="")
        p.start()
        self.addCleanup(p.stop)
        self.app._save_log()
        self.assertIn("不该落盘", self.app.txt_log.get("1.0", "end"))

    def test_a_callback_exception_goes_to_the_log_not_the_void(self):
        """tkinter 默认把回调异常打到 stderr，打包成 exe 后没人看得见。"""
        try:
            raise RuntimeError("回调里炸了")
        except RuntimeError:
            import sys

            self.app._on_callback_error(*sys.exc_info())
        self.assertIn("回调里炸了", self.app.txt_log.get("1.0", "end"))

    def test_clear_log_empties_the_panel(self):
        self.app.log("x")
        self.app._clear_log()
        self.assertEqual(self.app.txt_log.get("1.0", "end").strip(), "")


if __name__ == "__main__":
    unittest.main()
