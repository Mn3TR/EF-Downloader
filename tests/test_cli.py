"""CLI 参数解析测试。

这一组测试的价值在于**在用户之前发现参数层的问题**：
argparse 会把 help 字符串当 ``%`` 格式模板，一个没转义的百分号
（"占 96% 体积"）就会让整个 CLI 在构造 parser 时崩溃——而且
不构造 parser 的测试永远抓不到。所以这里把 --help 真的渲染一遍。
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

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

    def test_target_is_required(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.parser.parse_args(["plan"])

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


if __name__ == "__main__":
    unittest.main()
