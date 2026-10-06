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


if __name__ == "__main__":
    unittest.main()
