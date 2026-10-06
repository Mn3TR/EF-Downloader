"""``efd/ui/entry.py`` 与 ``efd/ui/app.py`` 里剩下的窗口杂项。

这两块是覆盖率报告最后剩下的真缺口：

- ``entry.py`` 57.1% —— ``detach_console`` 的 frozen 分支和 ``main()`` 全文；
- ``app.py`` 94.5% —— 回调异常兜底、保存日志、浏览/自动检测/打开目录。

``entry.main()`` 特别值得测：它是 exe 双击后唯一会走的路，一行写错就是
"双击没反应"——用户没有任何办法自己诊断。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from efd.ui import app as app_module
from efd.ui import entry

try:
    import tkinter as tk

    _root = tk.Tk()
    _root.destroy()
    _REASON = None
except Exception as exc:  # noqa: BLE001 - 无显示环境时整组跳过
    _REASON = f"没有可用的显示环境：{exc}"


class TestDetachConsole(unittest.TestCase):
    """``efd/ui/entry.py:detach_console``。"""

    def test_source_runs_do_nothing(self):
        """源码运行时不能摘控制台，否则 ``efd plan`` 的输出去哪了都不知道。"""
        with mock.patch.object(entry.sys, "frozen", False, create=True):
            with mock.patch.object(entry.ctypes, "windll", create=True) as windll:
                entry.detach_console()
        windll.kernel32.FreeConsole.assert_not_called()

    def test_a_frozen_process_releases_the_console(self):
        fake = mock.MagicMock(name="windll")
        with mock.patch.object(entry.sys, "frozen", True, create=True):
            with mock.patch.object(entry.ctypes, "windll", fake, create=True):
                entry.detach_console()
        fake.kernel32.FreeConsole.assert_called_once_with()

    def test_a_failing_freeconsole_is_not_fatal(self):
        """已经摘过一次、或没有控制台时，绝不能因此打不开界面。"""
        fake = mock.MagicMock(name="windll")
        fake.kernel32.FreeConsole.side_effect = OSError("没有控制台")
        with mock.patch.object(entry.sys, "frozen", True, create=True):
            with mock.patch.object(entry.ctypes, "windll", fake, create=True):
                entry.detach_console()  # 不该抛


class TestMain(unittest.TestCase):
    """``efd/ui/entry.py:main`` —— exe 双击后的唯一入口。"""

    def test_main_builds_a_window_and_runs_the_loop(self):
        with mock.patch.object(entry, "detach_console") as detach:
            with mock.patch.object(entry.ctypes, "windll", create=True) as windll:
                with mock.patch.object(entry, "App") as App:
                    entry.main()

        detach.assert_called_once_with()
        App.assert_called_once_with()
        App.return_value.mainloop.assert_called_once_with()
        # 高 DPI 声明要在建窗口之前发出去，否则窗口已经按旧 DPI 画好了
        self.assertTrue(windll.shcore.SetProcessDpiAwareness.called)

    def test_a_missing_shcore_is_not_fatal(self):
        """老系统上没有 shcore.dll：糊一点也要能用。"""
        windll = mock.MagicMock(name="windll")
        windll.shcore.SetProcessDpiAwareness.side_effect = OSError("没有这个 DLL")
        with mock.patch.object(entry, "detach_console"):
            with mock.patch.object(entry.ctypes, "windll", windll, create=True):
                with mock.patch.object(entry, "App") as App:
                    entry.main()
        App.return_value.mainloop.assert_called_once_with()

    def test_dpi_awareness_is_set_before_the_window_exists(self):
        order: list[str] = []
        windll = mock.MagicMock(name="windll")
        windll.shcore.SetProcessDpiAwareness.side_effect = lambda *_: order.append("dpi")
        App = mock.MagicMock(name="App")
        App.side_effect = lambda: order.append("window") or mock.MagicMock()

        with mock.patch.object(entry, "detach_console", lambda: order.append("console")):
            with mock.patch.object(entry.ctypes, "windll", windll, create=True):
                with mock.patch.object(entry, "App", App):
                    entry.main()

        self.assertEqual(order, ["console", "dpi", "window"])


@unittest.skipIf(_REASON is not None, _REASON or "")
class TestWindowExtras(unittest.TestCase):
    """``efd/ui/app.py`` 里没被点到的那些按钮和兜底。"""

    def setUp(self):
        self.app = app_module.App()
        self.addCleanup(self.app.destroy)
        self.info: list = []
        self.warn: list = []
        self.error: list = []
        for name, sink in (("showinfo", self.info),
                           ("showwarning", self.warn),
                           ("showerror", self.error)):
            # 两个参数（标题、正文），不能直接把 list.append 当 side_effect
            p = mock.patch.object(app_module.messagebox, name,
                                  mock.MagicMock(side_effect=lambda *a, _s=sink: _s.append(a)))
            p.start()
            self.addCleanup(p.stop)

    def test_a_callback_exception_lands_in_the_log(self):
        """tkinter 默认把回调异常打到 stderr——打包成 GUI 后那里什么都没有。"""
        try:
            raise ValueError("回调炸了")
        except ValueError:
            import sys

            self.app._on_callback_error(*sys.exc_info())
        text = self.app.txt_log.get("1.0", "end")
        self.assertIn("回调异常", text)
        self.assertIn("回调炸了", text)

    def test_a_broken_log_does_not_mask_the_exception(self):
        """记录异常的路径自己再炸，也不能把原异常吞掉。"""
        with mock.patch.object(self.app, "log", side_effect=RuntimeError("日志也坏了")):
            with mock.patch.object(app_module.traceback, "print_exception") as printer:
                try:
                    raise ValueError("原始异常")
                except ValueError:
                    import sys

                    self.app._on_callback_error(*sys.exc_info())
        printer.assert_called_once()

    def test_clear_log_empties_the_box(self):
        self.app.log("一些日志")
        self.assertTrue(self.app.txt_log.get("1.0", "end").strip())
        self.app._clear_log()
        self.assertEqual(self.app.txt_log.get("1.0", "end").strip(), "")

    def test_save_log_writes_utf8(self):
        self.app.log("中文日志 · 测试")
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "out.log")
            with mock.patch.object(app_module.filedialog, "asksaveasfilename",
                                   return_value=dest):
                self.app._save_log()
            with open(dest, encoding="utf-8") as fh:
                written = fh.read()
        self.assertIn("中文日志", written)
        self.assertIn("日志已保存到", self.app.txt_log.get("1.0", "end"))

    def test_save_log_cancelled_writes_nothing(self):
        with mock.patch.object(app_module.filedialog, "asksaveasfilename",
                               return_value=""):
            self.app._save_log()
        self.assertEqual(self.error, [], "取消不该弹错误框")

    def test_save_log_reports_an_unwritable_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "sub", "nope.log")  # 目录不存在
            with mock.patch.object(app_module.filedialog, "asksaveasfilename",
                                   return_value=target):
                self.app._save_log()
        self.assertEqual(len(self.error), 1)
        self.assertIn("保存失败", self.error[0][1])

    def test_browse_sets_a_normalised_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(app_module.filedialog, "askdirectory",
                                   return_value=tmp):
                self.app._browse()
        self.assertEqual(self.app.var_target.get(), os.path.normpath(tmp))

    def test_browse_cancelled_keeps_the_old_path(self):
        self.app.var_target.set("D:/keepme")
        with mock.patch.object(app_module.filedialog, "askdirectory", return_value=""):
            self.app._browse()
        self.assertEqual(self.app.var_target.get(), "D:/keepme")

    def test_autodetect_logs_the_found_directory(self):
        with mock.patch.object(app_module.detect, "suggest_target",
                               return_value="D:/Games/Endfield"):
            self.app._autodetect()
        self.assertEqual(self.app.var_target.get(), "D:/Games/Endfield")
        self.assertIn("检测到游戏目录", self.app.txt_log.get("1.0", "end"))

    def test_autodetect_tells_the_user_what_to_do_when_it_fails(self):
        with mock.patch.object(app_module.detect, "suggest_target", return_value=None):
            self.app._autodetect()
        self.assertIn("浏览", self.app.txt_log.get("1.0", "end"))

    def test_open_target_refuses_an_empty_path(self):
        self.app.var_target.set("")
        self.app._open_target()
        self.assertEqual(len(self.info), 1)

    def test_open_target_warns_about_a_missing_directory(self):
        self.app.var_target.set("D:/definitely/not/here/at/all")
        self.app._open_target()
        self.assertEqual(len(self.warn), 1)
        self.assertIn("目录不存在", self.warn[0][1])

    def test_open_target_opens_a_real_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.app.var_target.set(tmp)
            with mock.patch.object(app_module.os, "startfile",
                                   create=True) as startfile:
                self.app._open_target()
        startfile.assert_called_once()
        self.assertEqual(self.warn, [])

    def test_open_target_survives_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.app.var_target.set(tmp)
            with mock.patch.object(app_module.os, "startfile",
                                   create=True, side_effect=OSError("没有关联程序")):
                self.app._open_target()
        self.assertIn("打不开目录", self.app.txt_log.get("1.0", "end"))

    def test_disk_label_says_unknown_without_a_target(self):
        self.app.var_target.set("")
        self.app._closing = False
        self.app._refresh_disk()
        self.assertIn("—", self.app.lbl_disk.cget("text"))

    def test_destroy_cancels_pending_after_callbacks(self):
        """不取消的话，Tk 会在解释器销毁后继续触发它们。"""
        app = app_module.App()
        app.after(50, lambda: None)
        app.destroy()
        self.assertTrue(app._closing)


if __name__ == "__main__":
    unittest.main()
