"""GUI 构造测试。

不启动 ``mainloop``，只把窗口**真的建出来**再销毁——布局代码里的拼写错误、
控件参数错误、tkinter 用法错误，只有这样才抓得到。
没有显示环境时（CI / 无头）自动跳过。
"""

from __future__ import annotations

import unittest


def _display_available() -> str | None:
    try:
        import tkinter
    except ImportError as exc:  # pragma: no cover
        return f"没有 tkinter: {exc}"
    try:
        root = tkinter.Tk()
    except Exception as exc:  # noqa: BLE001 - TclError 等
        return f"没有可用的显示环境: {exc}"
    root.destroy()
    return None


_REASON = _display_available()


def state(widget) -> str:
    """ttk 控件读 ``["state"]`` 返回的是 Tcl index 对象，不是 str。"""
    return str(widget["state"])


@unittest.skipIf(_REASON is not None, _REASON or "")
class TestWindowConstruction(unittest.TestCase):
    def setUp(self):
        from efd.gui import App

        self.app = App()
        self.addCleanup(self.app.destroy)

    def test_title_is_ef_downloader(self):
        """窗口标题就是品牌名——叫「省空间安装器」没人知道这是什么。"""
        from efd.gui import APP_TITLE

        self.assertEqual(APP_TITLE, "EF DOWNLOADER")
        self.assertEqual(self.app.title(), "EF DOWNLOADER")

    def test_initial_state(self):
        self.assertIsNone(self.app.plan)
        self.assertIsNone(self.app.worker)
        self.assertEqual(state(self.app.btn_go), "disabled")
        self.assertEqual(state(self.app.btn_check), "normal")
        self.assertEqual(state(self.app.btn_stop), "disabled")

    def test_target_follows_settings_then_detection(self):
        """默认值来自「上次用过的 → 注册表探测」，不能是写死的开发者路径。"""
        from efd import detect, settings

        expected = settings.remembered_target() or detect.suggest_target()
        self.assertEqual(self.app.var_target.get(), expected)

    def test_target_is_never_a_hardcoded_dev_path(self):
        from efd import gui

        self.assertEqual(gui.DEFAULT_TARGET, "")

    def test_layout_runs(self):
        """触发一次布局与定时回调。"""
        self.app.update()
        self.app.update_idletasks()

    def test_log_and_plan_panels_accept_text(self):
        self.app.log("hello")
        self.assertIn("hello", self.app.txt_log.get("1.0", "end"))

        self.app.set_plan_text("计划内容")
        self.assertIn("计划内容", self.app.txt_plan.get("1.0", "end"))

    def test_busy_toggles_buttons(self):
        self.app._busy(True)
        self.assertEqual(state(self.app.btn_check), "disabled")
        self.assertEqual(state(self.app.btn_stop), "normal")

        self.app._busy(False)
        self.assertEqual(state(self.app.btn_check), "normal")
        # 没有计划时「开始安装」必须保持禁用
        self.assertEqual(state(self.app.btn_go), "disabled")

    def test_poll_keeps_rescheduling(self):
        """消息泵必须在没有消息时也把自己续上，否则界面会静默停摆。"""
        self.app.update()
        before = len(self.app.tk.call("after", "info"))
        self.assertGreater(before, 0, "消息泵没有挂上定时回调")


@unittest.skipIf(_REASON is not None, _REASON or "")
class TestPlanRendering(unittest.TestCase):
    """把计划渲染到面板上——纯显示逻辑，不需要网络。"""

    def setUp(self):
        from efd.archive import load_pack_sizes, open_local
        from efd.gui import App
        from pathlib import Path

        fixtures = Path(__file__).resolve().parent.parent / "data" / "fixtures"
        if not (fixtures / "vol054.bin").exists():
            self.skipTest("缺少离线夹具")
        self.archive = open_local(
            str(fixtures), load_pack_sizes(str(fixtures / "pack_sizes.json"))
        )
        self.addCleanup(self.archive.close)
        self.app = App()
        self.addCleanup(self.app.destroy)

        # 明确用一个临时目录当目标，不要依赖界面上预填的值
        # （探测失败时它是空串，空串会被 abspath 成当前目录）。
        import shutil
        import tempfile

        self.target = tempfile.mkdtemp(prefix="efd_gui_")
        self.addCleanup(shutil.rmtree, self.target, ignore_errors=True)

    def test_show_plan_renders_numbers(self):
        from efd.planner import make_plan

        plan = make_plan(self.archive, self.target)
        self.app._show_plan(plan)

        # 头部网格：一眼要能看到的数字
        self.assertEqual(self.app.v_version.cget("text"), plan.version)
        self.assertIn(str(plan.need_count), self.app.v_need.cget("text"))
        self.assertIn(str(plan.already_count), self.app.v_skip.cget("text"))
        self.assertTrue(self.app.v_save.cget("text").strip(), "省下多少是空的")

        # 详情：原始数字仍然完整可查
        text = self.app.txt_plan.get("1.0", "end")
        self.assertIn(str(plan.need_count), text)
        self.assertIn("峰值磁盘", text)

        # 计划到了，安装按钮就该解锁
        self.app._busy(False)
        self.assertEqual(state(self.app.btn_go), "normal")

    def test_plan_panel_reports_a_real_saving(self):
        """这个工具的卖点就是省磁盘，数字必须真的算出来。"""
        from efd.planner import make_plan
        from efd.util import human

        plan = make_plan(self.archive, self.target)
        self.app._show_plan(plan)
        self.assertGreater(plan.saving, 0)
        # v_peak 里是数值本身，「峰值磁盘」是旁边那格的行名
        self.assertIn("本工具", self.app.v_peak.cget("text"))
        self.assertIn("官方方式", self.app.v_peak.cget("text"))
        self.assertIn(human(plan.saving), self.app.v_save.cget("text"))
        self.assertEqual(self.app.lbl_pct.cget("text"), "0.0%")

    def test_detail_panel_toggles(self):
        self.assertFalse(self.app._detail_shown)
        self.app._toggle_detail()
        self.assertTrue(self.app._detail_shown)
        self.assertTrue(self.app.detail.winfo_manager(), "详情展开后没有真正上屏")
        self.app._toggle_detail()
        self.assertFalse(self.app._detail_shown)
        self.assertFalse(self.app.detail.winfo_manager(), "详情收起后没有真正下线")

    def test_progress_updates_bar_and_percent(self):
        from efd.installer import Progress

        p = Progress(
            done=53, total=1061,
            written=2_800_000_000, total_uncompressed=60_000_000_000,
            net_bytes=2_600_000_000, total_compressed=56_914_606_683,
            speed=5_600_000.0, eta=9000.0, current="Endfield_Data/x/y.bundle",
        )
        self.app._show_progress(p)
        # 进度条是整数刻度（maximum=1000，即 0.1% 分辨率）
        self.assertEqual(int(self.app.bar["value"]), int(p.fraction * 1000))
        self.assertIn("%", self.app.lbl_pct.cget("text"))
        self.assertIn("53/1061", self.app.lbl_stats.cget("text"))
        self.assertIn("y.bundle", self.app.lbl_cur.cget("text"))


@unittest.skipIf(_REASON is not None, _REASON or "")
class TestCheckUnlocksInstall(unittest.TestCase):
    """检查跑完之后，【开始安装】必须能点。

    v0.2.0 在这里有个 bug：检查分支发完计划就 ``return`` 了，没有任何一条
    「结束」消息，按钮于是永远停在忙碌态，用户只能重启程序。

    这里刻意把**真实的 Worker** 跑在离线夹具上，再用它**真实发出的消息序列**
    去驱动界面——如果手写消息序列，那这个测试就跟 Worker 脱钩了，
    以后 Worker 改坏它照样是绿的（当初就是这种测试放过了上面那个 bug）。
    """

    def setUp(self):
        from efd.archive import load_pack_sizes, open_local
        from efd.gui import App
        from pathlib import Path
        import shutil
        import tempfile

        fixtures = Path(__file__).resolve().parent.parent / "data" / "fixtures"
        if not (fixtures / "vol054.bin").exists():
            self.skipTest("缺少离线夹具")

        self.archive = open_local(
            str(fixtures), load_pack_sizes(str(fixtures / "pack_sizes.json"))
        )
        self.addCleanup(self.archive.close)
        self.app = App()
        self.addCleanup(self.app.destroy)
        self.target = tempfile.mkdtemp(prefix="efd_gui_check_")
        self.addCleanup(shutil.rmtree, self.target, ignore_errors=True)

    def _run_a_real_check(self) -> list[tuple[str, dict]]:
        """跑一次真的检查，返回 Worker 发出的消息。"""
        from unittest import mock

        from efd import gui

        worker = gui.Worker(self.app.q, self.target, False, False)
        with mock.patch.object(gui, "open_remote", return_value=self.archive):
            worker.run()  # 同步跑：不需要真的起线程，消息照样进队列

        messages = []
        while True:
            try:
                messages.append(self.app.q.get_nowait())
            except Exception:  # noqa: BLE001 - queue.Empty
                break
        return messages

    def test_check_run_emits_a_terminal_message(self):
        """检查这一轮必须以「结束」消息收尾，否则界面无从知道该解锁。"""
        from efd.gui import TERMINAL_KINDS

        messages = self._run_a_real_check()
        kinds = [k for k, _ in messages]

        errors = [p["text"] for k, p in messages if k == "error"]
        self.assertEqual(errors, [], f"检查过程报错了：{errors}")
        self.assertIn("plan", kinds, "没有把计划发回界面")
        self.assertIn(kinds[-1], TERMINAL_KINDS,
                      f"检查结束在 {kinds[-1]!r} 上，界面不会解锁（消息序列 {kinds}）")

    def test_check_message_sequence_unlocks_install(self):
        """把真实消息灌进真实的泵，【开始安装】必须解锁。

        ``worker`` 置空是刻意的：这样兜底的 watchdog 不会介入，
        解锁只能来自对结束消息的处理——测的正是当初漏掉的那条路径。
        """
        messages = self._run_a_real_check()

        # 模拟 _start(do_install=False)：先锁上，再等消息
        self.app.plan = None
        self.app.worker = None
        self.app._busy(True)
        self.assertEqual(state(self.app.btn_go), "disabled")

        for kind, payload in messages:
            self.app._handle(kind, payload)

        self.assertEqual(state(self.app.btn_check), "normal")
        self.assertEqual(state(self.app.btn_go), "normal",
                         "检查完成后【开始安装】仍然点不动")
        self.assertEqual(state(self.app.btn_stop), "disabled")
        self.assertTrue(self.app.plan is not None and self.app.plan.need_count > 0)

    def test_watchdog_unlocks_buttons_after_a_silent_death(self):
        """兜底：worker 悄悄死了、没发结束消息，界面也不能永远锁着。"""
        import threading

        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        self.app.worker = dead
        self.app._busy(True)

        self.app._poll()

        self.assertEqual(state(self.app.btn_check), "normal")
        self.assertEqual(state(self.app.btn_go), "disabled", "没有计划时不该解锁安装")

    def test_watchdog_leaves_a_live_worker_alone(self):
        """worker 还在跑的时候，兜底不能手贱去解锁。"""
        import threading

        gate = threading.Event()
        self.addCleanup(gate.set)
        live = threading.Thread(target=gate.wait, daemon=True)
        live.start()
        self.app.worker = live
        self.app._busy(True)

        self.app._poll()

        self.assertEqual(state(self.app.btn_check), "disabled")
        self.assertEqual(state(self.app.btn_stop), "normal")


if __name__ == "__main__":
    unittest.main()
