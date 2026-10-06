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

    def test_title(self):
        from efd.gui import APP_TITLE

        self.assertEqual(self.app.title(), APP_TITLE)

    def test_initial_state(self):
        self.assertIsNone(self.app.plan)
        self.assertIsNone(self.app.worker)
        self.assertEqual(state(self.app.btn_go), "disabled")
        self.assertEqual(state(self.app.btn_check), "normal")
        self.assertEqual(state(self.app.btn_stop), "disabled")

    def test_default_target_is_populated(self):
        from efd.gui import DEFAULT_TARGET

        self.assertEqual(self.app.var_target.get(), DEFAULT_TARGET)

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

    def test_show_plan_renders_numbers(self):
        from efd.planner import make_plan

        plan = make_plan(self.archive, str(self.app.var_target.get()))
        self.app._show_plan(plan)

        text = self.app.txt_plan.get("1.0", "end")
        self.assertIn(plan.version, text)
        self.assertIn(str(plan.need_count), text)
        self.assertIn("峰值磁盘", text)
        # 计划到了，安装按钮就该解锁
        self.app._busy(False)
        self.assertEqual(state(self.app.btn_go), "normal")


if __name__ == "__main__":
    unittest.main()
