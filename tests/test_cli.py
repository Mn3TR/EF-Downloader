"""CLI 参数解析测试。

这一组测试的价值在于**在用户之前发现参数层的问题**：
argparse 会把 help 字符串当 ``%`` 格式模板，一个没转义的百分号
（"占 96% 体积"）就会让整个 CLI 在构造 parser 时崩溃——而且
不构造 parser 的测试永远抓不到。所以这里把 --help 真的渲染一遍。
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

from efd import __version__
from efd.cli import build_parser, main

SUBCOMMANDS = ("plan", "install", "probe", "gui")

TARGET = r"D:\some\where"


class TestParserBuilds(unittest.TestCase):
    def test_build_does_not_raise(self):
        self.assertIsNotNone(build_parser())

    def test_help_renders_for_every_subcommand(self):
        """任何 help 字符串里的裸 % 都会在这里炸出来。"""
        parser = build_parser()
        self.assertIn("efd", parser.format_help())
        for name in SUBCOMMANDS:
            with self.subTest(command=name):
                sub = parser._subparsers._group_actions[0].choices[name]
                text = sub.format_help()
                self.assertGreater(len(text), 0)


class TestParsing(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_plan_defaults(self):
        args = self.parser.parse_args(["plan", "--target", TARGET])
        self.assertEqual(args.command, "plan")
        self.assertEqual(args.target, TARGET)
        self.assertFalse(args.verify_crc)
        self.assertFalse(args.force)
        self.assertEqual(args.limit, 0)
        self.assertEqual(args.exclude, "")
        self.assertEqual(args.block, 1 << 20)

    def test_install_requires_apply_flag_field(self):
        args = self.parser.parse_args(["install", "--target", TARGET])
        self.assertFalse(args.apply)

    def test_install_apply(self):
        args = self.parser.parse_args(["install", "--target", TARGET, "--apply"])
        self.assertTrue(args.apply)

    def test_probe_defaults(self):
        args = self.parser.parse_args(["probe"])
        self.assertEqual(args.count, 3)

    def test_gui_takes_no_target(self):
        args = self.parser.parse_args(["gui"])
        self.assertEqual(args.command, "gui")
        self.assertFalse(hasattr(args, "target"))

    def test_target_is_optional_at_parse_time(self):
        """``--target`` 不再是 argparse 层面的必填项。

        「必须有个目标」这件事挪到了运行时（``cli._resolve_target``），
        因为探测结果只有那时才知道。
        """
        args = self.parser.parse_args(["plan"])
        self.assertIsNone(args.target)

    def test_command_is_required(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.parser.parse_args([])

    def test_unknown_command(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.parser.parse_args(["frobnicate"])


class TestExcludeResolution(unittest.TestCase):
    def _resolve(self, argv):
        from efd.cli import _resolve_excludes
        return _resolve_excludes(self.parser.parse_args(argv))

    def setUp(self):
        self.parser = build_parser()

    def test_default_excludes_nothing(self):
        """默认必须什么都不排除——这是已实测验证过的行为。"""
        self.assertEqual(self._resolve(["plan", "--target", TARGET]), ())

    def test_explicit_prefixes(self):
        got = self._resolve(["plan", "--target", TARGET, "--exclude", "a/,b/"])
        self.assertEqual(got, ("a/", "b/"))

    def test_exclude_ace_preset(self):
        from efd.config import EXCLUDE_ACE
        got = self._resolve(["plan", "--target", TARGET, "--exclude-ace"])
        self.assertEqual(set(got), set(EXCLUDE_ACE))

    def test_exclude_streaming_preset(self):
        from efd.config import EXCLUDE_STREAMING
        got = self._resolve(["plan", "--target", TARGET, "--exclude-streaming"])
        self.assertEqual(set(got), set(EXCLUDE_STREAMING))

    def test_presets_combine_with_explicit(self):
        got = self._resolve(
            ["plan", "--target", TARGET, "--exclude", "x/", "--exclude-ace"]
        )
        self.assertIn("x/", got)
        self.assertIn("AntiCheatExpert/", got)

    def test_empty_exclude_string_is_ignored(self):
        self.assertEqual(self._resolve(["plan", "--target", TARGET, "--exclude", ",,"]), ())


class TestEntryPoints(unittest.TestCase):
    def test_version_flag(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(buf):
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("efd", buf.getvalue())

    def test_no_args_exits_nonzero(self):
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(io.StringIO()):
            main([])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_gui_command_is_routed(self):
        """确认 gui 子命令被正确路由，而不是真的弹出一个窗口。

        gui 模块在函数内部才 import，所以替换 efd.gui.main 就能拦住。
        """
        import efd.gui
        import efd.cli

        calls = []
        original = efd.gui.main
        efd.gui.main = lambda: calls.append(1)
        try:
            self.assertEqual(main(["gui"]), 0)
        finally:
            efd.gui.main = original
        self.assertEqual(calls, [1], "gui 子命令没有调用 efd.gui.main")


class TestResolveTarget(unittest.TestCase):
    """目标目录的确定规则。

    这是会往盘里写 58 GB 的参数，所以「探测不到时怎么办」必须钉死：
    **报错，绝不猜**。
    """

    @staticmethod
    def _args(target=None):
        from types import SimpleNamespace

        return SimpleNamespace(target=target)

    def test_explicit_target_wins(self):
        from efd.cli import _resolve_target

        self.assertEqual(_resolve_target(self._args("X:/y")), "X:/y")

    def test_falls_back_to_detection(self):
        from efd import cli, detect

        with mock.patch.object(detect, "suggest_target", return_value="D:/found"):
            with redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(cli._resolve_target(self._args()), "D:/found")
        self.assertIn("自动探测到", buf.getvalue())

    def test_explicit_target_does_not_trigger_detection(self):
        from efd import cli, detect

        with mock.patch.object(detect, "suggest_target") as spy:
            cli._resolve_target(self._args("X:/y"))
        spy.assert_not_called()

    def test_errors_when_nothing_found(self):
        from efd import cli, detect

        with mock.patch.object(detect, "suggest_target", return_value=""):
            with self.assertRaises(SystemExit) as ctx:
                cli._resolve_target(self._args())
        self.assertIn("--target", str(ctx.exception))

    def test_never_falls_back_to_cwd(self):
        """兜底成当前目录是最危险的做法——那会把 58 GB 写进随便什么地方。"""
        from efd import cli, detect

        with mock.patch.object(detect, "suggest_target", return_value=""):
            with self.assertRaises(SystemExit):
                cli._resolve_target(self._args())


class TestFrozenBehaviour(unittest.TestCase):
    """打包成 exe 后的行为。用 sys.frozen 模拟冻结态，不需要真的构建。"""

    def setUp(self):
        import efd.gui

        self.gui = efd.gui
        self.original_main = efd.gui.main
        self.saved_argv = sys.argv

    def tearDown(self):
        self.gui.main = self.original_main
        sys.argv = self.saved_argv
        if hasattr(sys, "frozen"):
            del sys.frozen

    def test_frozen_no_args_defaults_to_gui(self):
        """双击 exe 没有任何参数 —— 这是「双击」唯一合理的语义。"""
        import efd.cli

        calls = []
        self.gui.main = lambda: calls.append(1)
        sys.argv = ["EFD.exe"]
        sys.frozen = True  # type: ignore[attr-defined]

        self.assertEqual(efd.cli.main(), 0)
        self.assertEqual(calls, [1])

    def test_not_frozen_no_args_still_requires_subcommand(self):
        """源码运行时行为不变：仍然报错要求子命令。"""
        import efd.cli

        sys.argv = []
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(io.StringIO()):
            efd.cli.main()
        self.assertNotEqual(ctx.exception.code, 0)

    def test_frozen_with_args_does_not_force_gui(self):
        """冻结态下带了参数就必须走正常解析，不能被改写成 gui。"""
        import efd.cli

        calls = []
        self.gui.main = lambda: calls.append(1)
        sys.argv = ["EFD.exe", "--version"]
        sys.frozen = True  # type: ignore[attr-defined]

        with self.assertRaises(SystemExit) as ctx, redirect_stdout(io.StringIO()):
            efd.cli.main()
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(calls, [], "带参数时不该进 GUI")


class TestDetachConsole(unittest.TestCase):
    def test_noop_when_not_frozen(self):
        """源码运行时 detach_console 必须什么都不做。

        真的 FreeConsole 会把这个测试进程从控制台摘下来，之后所有输出都看不见。
        """
        from efd.gui import detach_console

        if hasattr(sys, "frozen"):
            self.skipTest("当前进程已被冻结")
        detach_console()  # 不抛异常即可
        print("控制台仍然可用")


class TestOutputEncoding(unittest.TestCase):
    """管道/重定向场景必须写 UTF-8。

    跑的是**真的子进程 + 真的管道**，所以能抓住「在控制台里看着正常、
    一重定向就变成 U+FFFD」这类只在特定消费方那里才暴露的问题。
    """

    def test_piped_help_is_decodable_as_utf8(self):
        if os.name != "nt":
            self.skipTest("这套编码处理只针对 Windows")
        proc = subprocess.run(
            [sys.executable, "-m", "efd", "--help"],
            capture_output=True, cwd=ROOT,
        )
        self.assertEqual(proc.returncode, 0)
        text = proc.stdout.decode("utf-8")  # 不是 UTF-8 就会在这里炸
        self.assertIn("省空间", text)
        for name in SUBCOMMANDS:
            self.assertIn(name, text)

    def test_piped_version_is_utf8(self):
        proc = subprocess.run(
            [sys.executable, "-m", "efd", "--version"],
            capture_output=True, cwd=ROOT,
        )
        self.assertEqual(proc.returncode, 0)
        # 跟着 __version__ 走，别把版本号在测试里再抄一份——那样每次发版
        # 都得改测试，改漏了就是假失败。
        self.assertEqual(proc.stdout.decode("utf-8").strip(), f"efd {__version__}")

    def test_errors_are_utf8_too(self):
        proc = subprocess.run(
            [sys.executable, "-m", "efd"],
            capture_output=True, cwd=ROOT,
        )
        self.assertNotEqual(proc.returncode, 0)
        # argparse 的用法提示里带中文（description），必须能按 UTF-8 解开
        proc.stderr.decode("utf-8")


if __name__ == "__main__":
    unittest.main()
