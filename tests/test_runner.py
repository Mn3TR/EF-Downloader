"""``efd/cli/runner.py`` 的退出码契约测试。

``runner.main`` 是**唯一**把异常翻译成退出码的地方，而退出码会被外壳脚本、
批处理和用户自己写的 ``if errorlevel`` 判断读取。此前这条翻译链完全没测过
（覆盖率 67.9%，缺的正好是四个 except 分支）。

这里的重点不是"有没有打印错误"，而是**每种异常映射到哪个退出码**：
- 用户按 Ctrl+C 是 ``EXIT_PARTIAL(2)``——不是失败，重跑就能续传；
- 接口/下载/系统错误是 ``EXIT_ERROR(1)``；
- 成功是 ``EXIT_OK(0)``。

把 2 和 1 弄混会让"用户主动中断"看起来像"程序坏了"。
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from efd.cli import commands, runner
from efd.cli.exitcode import EXIT_ERROR, EXIT_OK, EXIT_PARTIAL
from efd.core.seed import SeedError
from efd.net import RangeError


def run_main(argv, *, side_effect=None, retval=EXIT_OK) -> tuple[int, str, str]:
    """跑 ``runner.main``，返回 (退出码, stdout, stderr)。

    四个子命令**全部**换成同一个 mock：只换 ``plan`` 的话，用例一旦写成
    ``["install", ...]`` 就会悄悄跑起真的 ``cmd_install``（会去连网），
    测出来的东西和以为测的完全不是一回事。
    """
    out, err = io.StringIO(), io.StringIO()
    fake = mock.Mock(return_value=retval, side_effect=side_effect)
    with mock.patch.dict(runner.COMMANDS, {k: fake for k in runner.COMMANDS}):
        with redirect_stdout(out), redirect_stderr(err):
            code = runner.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestExitCodeContract(unittest.TestCase):
    """三个退出码是对外的契约，构建脚本和批处理都按它判断。"""

    def test_values_are_the_documented_ones(self):
        self.assertEqual(EXIT_OK, 0)
        self.assertEqual(EXIT_ERROR, 1)
        self.assertEqual(EXIT_PARTIAL, 2)

    def test_they_are_distinct(self):
        self.assertEqual(len({EXIT_OK, EXIT_ERROR, EXIT_PARTIAL}), 3)


class TestSuccessfulRun(unittest.TestCase):
    def test_returns_the_command_exit_code(self):
        code, _, _ = run_main(["plan", "--target", "X:/y"])
        self.assertEqual(code, EXIT_OK)

    def test_a_nonzero_command_code_is_passed_through(self):
        """命令层返回 2（部分失败）时不能被吞成 0。"""
        code, _, _ = run_main(["plan", "--target", "X:/y"], retval=EXIT_PARTIAL)
        self.assertEqual(code, EXIT_PARTIAL)

    def test_dispatches_to_the_right_command(self):
        sentinel = mock.Mock(return_value=EXIT_OK)
        with mock.patch.dict(runner.COMMANDS, {"install": sentinel}):
            with redirect_stdout(io.StringIO()):
                runner.main(["install", "--target", "X:/y", "--apply"])
        sentinel.assert_called_once()

    def test_every_subcommand_is_registered(self):
        """argparse 认识但 COMMANDS 里没有 → KeyError，用户看到的是崩溃。"""
        from efd.cli import build_parser

        parser = build_parser()
        choices = parser._subparsers._group_actions[0].choices
        self.assertEqual(set(runner.COMMANDS), set(choices))


class TestExceptionTranslation(unittest.TestCase):
    def test_keyboard_interrupt_is_partial_not_error(self):
        """Ctrl+C 是用户的选择，不是故障——而且重跑能续传。"""
        code, out, _ = run_main(["plan", "--target", "X:/y"],
                                side_effect=KeyboardInterrupt())
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertIn("续传", out)

    def test_seed_error_is_an_error_and_explains_itself(self):
        code, _, err = run_main(["plan", "--target", "X:/y"],
                                side_effect=SeedError("接口 500"))
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("接口错误", err)
        self.assertIn("接口 500", err)
        # 这条提示是给"服务端结构变了"这种不可避免的情况用的
        self.assertIn("不是公开 API", err)

    def test_range_error_is_an_error(self):
        code, _, err = run_main(["install", "--target", "X:/y"],
                                side_effect=RangeError("CDN 不支持范围请求"))
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("下载错误", err)
        self.assertIn("CDN 不支持范围请求", err)

    def test_oserror_is_an_error(self):
        code, _, err = run_main(["install", "--target", "X:/y"],
                                side_effect=OSError("磁盘已满"))
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("系统错误", err)
        self.assertIn("磁盘已满", err)

    def test_errors_go_to_stderr_not_stdout(self):
        """错误必须走 stderr：管道里 stdout 是数据，混进去就毁了它。"""
        _, out, err = run_main(["plan", "--target", "X:/y"],
                               side_effect=SeedError("boom"))
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")

    def test_unexpected_exception_is_not_swallowed(self):
        """没预料到的异常要原样抛出（带 traceback），不能被翻译成"正常退出"。"""
        with self.assertRaises(ValueError):
            run_main(["plan", "--target", "X:/y"], side_effect=ValueError("意料之外"))

    def test_seed_error_is_a_runtime_error_subclass(self):
        """捕获顺序依赖继承关系：SeedError 必须能被 OSError 之外的方式区分。"""
        self.assertTrue(issubclass(SeedError, RuntimeError))
        self.assertFalse(issubclass(SeedError, OSError))

    def test_range_error_is_not_an_oserror(self):
        """RangeError 若继承 OSError，上面的分支顺序会把提示说错。"""
        self.assertFalse(issubclass(RangeError, OSError))


class TestFrozenDefaults(unittest.TestCase):
    """冻结态下没参数就进 GUI——``runner`` 负责这个改写。"""

    def setUp(self):
        import sys

        self.sys = sys
        self.saved_argv = sys.argv
        self.addCleanup(self._restore)

    def _restore(self):
        self.sys.argv = self.saved_argv
        if hasattr(self.sys, "frozen"):
            del self.sys.frozen

    def test_frozen_no_args_becomes_gui(self):
        seen = {}

        def fake_gui(args):
            seen["command"] = args.command
            return EXIT_OK

        self.sys.argv = ["EFD.exe"]
        self.sys.frozen = True
        with mock.patch.dict(runner.COMMANDS, {"gui": fake_gui}):
            with redirect_stdout(io.StringIO()):
                code = runner.main()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(seen.get("command"), "gui")

    def test_not_frozen_no_args_is_an_error(self):
        self.sys.argv = []
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            runner.main()

    def test_explicit_argv_is_never_rewritten(self):
        """显式传 argv（测试、被其他程序调用）不受 frozen 影响。"""
        self.sys.argv = ["EFD.exe"]
        self.sys.frozen = True
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            runner.main(["--version"])


class TestMainIsSelfContained(unittest.TestCase):
    def test_main_sets_up_output_encoding(self):
        """Windows 管道默认不是 UTF-8；不设就会把中文写成 U+FFFD。"""
        with mock.patch.object(runner, "setup_output_encoding") as spy:
            with mock.patch.dict(runner.COMMANDS,
                                 {"plan": mock.Mock(return_value=EXIT_OK)}):
                with redirect_stdout(io.StringIO()):
                    runner.main(["plan", "--target", "X:/y"])
        spy.assert_called_once()

    def test_version_flag_exits_through_argparse(self):
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(io.StringIO()):
            runner.main(["--version"])
        self.assertEqual(ctx.exception.code, EXIT_OK)


class TestCommandsTableCoversReality(unittest.TestCase):
    def test_table_points_at_the_real_functions(self):
        self.assertIs(runner.COMMANDS["plan"], commands.cmd_plan)
        self.assertIs(runner.COMMANDS["install"], commands.cmd_install)
        self.assertIs(runner.COMMANDS["probe"], commands.cmd_probe)
        self.assertIs(runner.COMMANDS["gui"], commands.cmd_gui)


if __name__ == "__main__":
    unittest.main()
