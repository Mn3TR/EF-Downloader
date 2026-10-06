"""GUI 构造测试。

不启动 ``mainloop``，只把窗口**真的建出来**再销毁——布局代码里的拼写错误、
控件参数错误、tkinter 用法错误，只有这样才抓得到。
没有显示环境时（CI / 无头）自动跳过。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
        from efd.ui import App

        self.app = App()
        self.addCleanup(self.app.destroy)

    def test_title_is_ef_downloader(self):
        """窗口标题就是品牌名——叫「省空间安装器」没人知道这是什么。"""
        from efd.ui.theme import APP_TITLE

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
        from efd.core import detect, settings

        expected = settings.remembered_target() or detect.suggest_target()
        self.assertEqual(self.app.var_target.get(), expected)

    def test_target_is_never_a_hardcoded_dev_path(self):
        from efd.ui import theme as gui

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
        from efd.core.archive import load_pack_sizes, open_local
        from efd.ui import App
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
        from efd.core.planner import make_plan

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
        from efd.core.planner import make_plan
        from efd.core.util import human

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
        from efd.core.installer import Progress

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
        from efd.core.archive import load_pack_sizes, open_local
        from efd.ui import App
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

        from efd.ui import messages

        worker = messages.Worker(self.app.q, self.target, False, False)
        with mock.patch.object(messages, "open_remote", return_value=self.archive):
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
        from efd.ui import TERMINAL_KINDS

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


@unittest.skipIf(_REASON is not None, _REASON or "")
class TestFinishTellsTheTruth(unittest.TestCase):
    """收尾时必须如实报告「装完了」还是「中途断了」。

    v0.2.3 之前这里有个 bug：``install()`` 在第一个失败处就提前返回
    （GUI 从不传 ``keep_going``），剩下的文件一个没碰，而 ``_finish`` 只看
    ``result.errors`` 有没有内容来决定**日志文案**，进度条和弹窗却是无条件
    的「100% + 安装完成」。于是网络抖一下——实测 400 次 Range 请求里就有
    1 次 TLS 断流——界面就宣称装完了，用户以为好了，其实一个大文件都没装成。

    这组测试只管一件事：**有错的时候绝不能说完成。**
    """

    def setUp(self):
        from efd.ui import App
        from efd.ui import handlers

        fixtures = Path(__file__).resolve().parent.parent / "data" / "fixtures"
        if not (fixtures / "vol054.bin").exists():
            self.skipTest("缺少离线夹具")

        from efd.core.archive import load_pack_sizes, open_local
        from efd.core.planner import make_plan

        archive = open_local(
            str(fixtures), load_pack_sizes(str(fixtures / "pack_sizes.json"))
        )
        self.addCleanup(archive.close)
        self.app = App()
        self.addCleanup(self.app.destroy)
        self.plan = make_plan(archive, tempfile.mkdtemp(prefix="efd_gui_fin_"))
        self.addCleanup(shutil.rmtree, self.plan.target, ignore_errors=True)
        self.assertGreater(self.plan.need_count, 0, "夹具里没有待装文件")

        self.info: list[tuple] = []
        self.warn: list[tuple] = []
        for name, sink in (("showinfo", self.info), ("showwarning", self.warn),
                           ("showerror", self.warn)):
            p = mock.patch.object(handlers.messagebox, name,
                                  side_effect=lambda *a, _s=sink, **k: _s.append(a))
            p.start()
            self.addCleanup(p.stop)

    def result(self, **kw):
        from efd.core.installer import Result

        base = dict(done=0, written=0, net_bytes=0, elapsed=1.0, errors=[])
        base.update(kw)
        return Result(**base)

    def test_a_failed_run_never_claims_success(self):
        self.app._finish(
            self.result(errors=["Endfield_Data/x/y.chk: <urlopen error EOF>"]),
            self.plan, stopped=False,
        )
        self.assertEqual(self.info, [], "有错误还弹了「安装完成」")
        self.assertEqual(len(self.warn), 1, "有错误必须弹一个「未完成」的警告")
        self.assertEqual(self.warn[0][0], "未完成")
        self.assertIn("没装", str(self.warn[0][1]), "没告诉用户还剩多少没装")

    def test_a_failed_run_does_not_paint_a_full_bar(self):
        """进度条画到 100% 就是那句谎话的源头，用户主要看的就是它。"""
        plan = self.plan
        self.app._finish(
            self.result(done=7, errors=["boom"]), plan, stopped=False,
        )
        self.assertLess(int(self.app.bar["value"]), 1000, "失败了还画满进度条")
        self.assertNotEqual(self.app.lbl_pct.cget("text"), "100.0%")
        self.assertIn("7", self.app.lbl_stats.cget("text"))
        self.assertIn(str(plan.need_count), self.app.lbl_stats.cget("text"))

    def test_a_failed_run_surfaces_the_error_text_in_the_log(self):
        self.app._finish(
            self.result(errors=["卷 013 对 Range bytes=0-1023 返回了 502（期望 206）。"]),
            self.plan, stopped=False,
        )
        text = self.app.txt_log.get("1.0", "end")
        self.assertIn("502", text, "错误详情没进日志")
        self.assertNotIn("✅", text, "又打了那个成功对勾")

    def test_a_clean_run_still_says_complete(self):
        """另一半：真的装完了必须照常报完成，别矫枉过正。"""
        self.app._finish(self.result(done=3, written=1234), self.plan, stopped=False)
        self.assertEqual(int(self.app.bar["value"]), 1000)
        self.assertEqual(self.app.lbl_pct.cget("text"), "100.0%")
        self.assertEqual(self.warn, [], "干净跑完不该弹警告")
        self.assertEqual(len(self.info), 1)
        self.assertIn("安装完成", str(self.info[0][1]))

    def test_a_stop_is_not_reported_as_either(self):
        """停止是用户自己的选择，既不是完成也不是失败。"""
        self.app._finish(self.result(done=2, written=99, stopped=True),
                         self.plan, stopped=True)
        self.assertEqual(self.info, [])
        self.assertEqual(self.warn, [])
        self.assertIn("已停止", self.app.lbl_state.cget("text"))


if __name__ == "__main__":
    unittest.main()
