"""``efd/cli/commands.py`` 的编排测试。

这一层是四个子命令的实现，此前覆盖率 18.7% —— 意味着 ``--json`` 写没写、
``--apply`` 之前会不会真的动手、失败时退出码是几，全都没有测试盯着。

它很适合测试，因为**依赖都是模块级名字**（``open_remote`` / ``make_plan`` /
``install``），替换掉就能把编排逻辑单独跑一遍，不需要网络也不需要磁盘。
这里刻意**不**替换 ``print_plan`` 之类的渲染函数：让真的渲染跑过去，
能顺带确认命令在真实计划上不会因渲染而崩。

最要命的三条不变量，每条都有一个专门的用例：
1. 不加 ``--apply`` 绝不动手写盘；
2. 有失败必须返回 ``EXIT_PARTIAL``，不能被当成成功；
3. ``--json`` 写出的必须是**全量**计划（截断过的计划会骗人）。
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from efd.cli import commands
from efd.cli.exitcode import EXIT_ERROR, EXIT_OK, EXIT_PARTIAL
from efd.core.archive import Entry
from efd.core.installer import Progress, Result
from efd.core.planner import Plan


def entry(name: str, size: int, compressed: int = 0, *, offset: int = 0,
          method: int = 8, is_dir: bool = False) -> Entry:
    return Entry(name=name, size=size, compressed=compressed, crc=0,
                 offset=offset, method=method, is_dir=is_dir)


class FakeArchive:
    """够 render 层用的最小归档替身，同时是个可用的上下文管理器。"""

    def __init__(self, files=(), *, version="1.5.3", total_bytes=0, net_bytes=0,
                 net_requests=0, volumes=54, prefetched=0, wasted=0, limiter=None):
        self.version = version
        self.total_bytes = total_bytes
        self.net_bytes = net_bytes
        self.net_requests = net_requests
        self.volumes = list(range(volumes))
        self.prefetched_bytes = prefetched
        self.prefetch_wasted_bytes = wasted
        self.limiter = limiter
        self._files = list(files) + [entry("dir/", 0, is_dir=True)]
        self.entries = list(self._files)
        self.read_calls: list[Entry] = []

    def files(self):
        return [e for e in self._files if not e.is_dir]

    def dirs(self):
        return [e for e in self._files if e.is_dir]

    def read(self, e):
        self.read_calls.append(e)
        return b"x" * e.size

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def plan_for(target: str = r"D:\games", *, need=None, **kw) -> Plan:
    need = [entry("a.bin", 2048, 1024)] if need is None else need
    return Plan(
        version="1.5.3", target=target, archive_bytes=50_000,
        manifest_bytes=414_909, entries_total=3,
        already=[entry("kept.bin", 100)], need=need, **kw,
    )


def base_args(**kw) -> SimpleNamespace:
    """``plan`` 子命令解析后的默认参数集，按需覆盖。"""
    args = dict(
        command="plan", target=r"D:\games", exclude="", exclude_ace=False,
        exclude_streaming=False, verify_crc=False, force=False, limit=0,
        block=1 << 20, timeout=60, jobs=8, limit_rate="0",
        json=None, stale=False, quiet=False,
    )
    args.update(kw)
    return SimpleNamespace(**args)


def install_args(**kw) -> SimpleNamespace:
    args = base_args(**kw)
    args.command = "install"
    args.apply = kw.get("apply", False)
    args.prune = kw.get("prune", False)
    args.keep_going = kw.get("keep_going", False)
    return args


class Patched:
    """把 commands 的三个模块级依赖一起换掉，任何没预期的调用都会炸。"""

    def __init__(self, archive=None, plan=None, result=None):
        self.archive = archive if archive is not None else FakeArchive()
        self.plan = plan if plan is not None else plan_for()
        self.result = result if result is not None else Result()
        self.patches = [
            mock.patch.object(commands, "open_remote", return_value=self.archive),
            mock.patch.object(commands, "make_plan", return_value=self.plan),
            mock.patch.object(commands, "install", return_value=self.result),
        ]

    def __enter__(self):
        self.mocks = [p.start() for p in self.patches]
        self.open_remote, self.make_plan, self.install = self.mocks
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        return False


def run(func, args) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        func(args)
    return buf.getvalue()


class QuietStdout(unittest.TestCase):
    """直接调用 cmd_* 的用例会把整份渲染结果打进测试输出。

    单独跑某个用例调试时那些文本有用，但在整套里就是噪音——统一收进缓冲，
    需要断言的用例仍旧用 ``run()`` 自己拿。
    """

    def setUp(self):
        self._sink = io.StringIO()
        self._redirect = redirect_stdout(self._sink)
        self._redirect.__enter__()

    def tearDown(self):
        self._redirect.__exit__(None, None, None)


class TestCmdPlan(QuietStdout):
    def test_returns_ok(self):
        with Patched():
            self.assertEqual(commands.cmd_plan(base_args()), EXIT_OK)

    def test_never_installs(self):
        """``plan`` 的整个存在意义就是"绝不写游戏文件"。"""
        with Patched() as p:
            commands.cmd_plan(base_args())
        p.install.assert_not_called()

    def test_passes_scope_options_through_to_the_planner(self):
        with Patched() as p:
            commands.cmd_plan(base_args(
                exclude="x/,y/", exclude_ace=True, verify_crc=True,
                force=True, limit=7, stale=True,
            ))
        kw = p.make_plan.call_args.kwargs
        self.assertIn("x/", kw["exclude_prefixes"])
        self.assertIn("AntiCheatExpert/", kw["exclude_prefixes"])
        self.assertTrue(kw["verify_crc"])
        self.assertTrue(kw["force"])
        self.assertEqual(kw["limit"], 7)
        self.assertTrue(kw["detect_stale"])

    def test_stale_flag_maps_to_detect_stale_not_prune(self):
        """``plan --stale`` 是"扫出多余文件"，不是"删掉它们"。"""
        with Patched() as p:
            commands.cmd_plan(base_args(stale=True))
        self.assertTrue(p.make_plan.call_args.kwargs["detect_stale"])
        self.assertNotIn("prune", p.make_plan.call_args.kwargs)

    def test_prints_the_dry_run_notice(self):
        with Patched():
            out = run(commands.cmd_plan, base_args())
        self.assertIn("dry-run", out)
        self.assertIn("未修改任何文件", out)

    def test_quiet_skips_the_header_but_still_prints_the_plan(self):
        with Patched():
            out = run(commands.cmd_plan, base_args(quiet=True))
        self.assertNotIn("归档逻辑长度", out)
        self.assertIn("===== 计划 =====", out)

    def test_loud_prints_the_header_and_distribution(self):
        with Patched(archive=FakeArchive([entry("Endfield_Data/a.bin", 100, 50)])):
            out = run(commands.cmd_plan, base_args())
        self.assertIn("归档逻辑长度", out)
        self.assertIn("顶层分布", out)

    def test_block_and_net_options_reach_open_remote(self):
        with Patched() as p:
            commands.cmd_plan(base_args(block=1 << 16, jobs=4, limit_rate="2M"))
        kw = p.open_remote.call_args.kwargs
        self.assertEqual(kw["block"], 1 << 16)
        self.assertEqual(kw["jobs"], 4)
        self.assertEqual(kw["limit_rate"], 2 * 1024 * 1024)

    def test_no_json_flag_writes_nothing(self):
        with Patched():
            out = run(commands.cmd_plan, base_args())
        self.assertNotIn("完整计划已写出", out)

    def test_json_flag_writes_a_full_plan(self):
        plan = plan_for(need=[entry(f"f{i}.bin", 10) for i in range(60)])
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "plan.json"
            with Patched(plan=plan):
                out = run(commands.cmd_plan, base_args(json=str(dest)))
            self.assertIn("完整计划已写出", out)
            data = json.loads(dest.read_text(encoding="utf-8"))
        # 截断过的「计划」会骗人 —— 60 个必须一个不少。
        self.assertEqual(len(data["need"]), 60)
        self.assertEqual(data["need_count"], 60)
        self.assertEqual(data["target"], plan.target)

    def test_json_is_written_as_readable_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "计划.json"
            with Patched(plan=plan_for()):
                commands.cmd_plan(base_args(json=str(dest)))
            raw = dest.read_bytes()
        raw.decode("utf-8")  # 不抛即通过
        self.assertIn("[", raw.decode("utf-8"))


class TestCmdInstall(QuietStdout):
    def test_without_apply_is_a_dry_run(self):
        """默认必须只预览。这条要是坏了，用户敲一下命令就开始写 58 GB。"""
        with Patched() as p:
            code = commands.cmd_install(install_args(apply=False))
        self.assertEqual(code, EXIT_OK)
        p.install.assert_not_called()

    def test_dry_run_prints_the_target_and_the_hint(self):
        with Patched():
            out = run(commands.cmd_install, install_args(apply=False))
        self.assertIn("dry-run", out)
        self.assertIn("--apply", out)
        self.assertIn(r"D:\games", out)

    def test_apply_calls_install(self):
        with Patched() as p:
            commands.cmd_install(install_args(apply=True))
        p.install.assert_called_once()

    def test_apply_passes_keep_going_and_prune(self):
        with Patched() as p:
            commands.cmd_install(install_args(apply=True, keep_going=True, prune=True))
        kw = p.install.call_args.kwargs
        self.assertTrue(kw["keep_going"])
        self.assertTrue(kw["prune"])

    def test_prune_flag_also_drives_the_stale_scan(self):
        """``--prune`` 要删的多余文件，得先由 ``detect_stale`` 找出来。"""
        with Patched() as p:
            commands.cmd_install(install_args(apply=True, prune=True))
        self.assertTrue(p.make_plan.call_args.kwargs["detect_stale"])

    def test_apply_uses_a_long_progress_interval(self):
        """CLI 的进度是逐行打印的，0.5 秒一行会把终端刷爆。"""
        with Patched() as p:
            commands.cmd_install(install_args(apply=True))
        self.assertEqual(p.install.call_args.kwargs["interval"], 10.0)

    def test_progress_callback_prints_done_written_speed_and_eta(self):
        captured = {}

        def fake_install(archive, plan, *, on_progress, **kw):
            captured["cb"] = on_progress
            return Result()

        with Patched() as p:
            p.install.side_effect = fake_install
            commands.cmd_install(install_args(apply=True))

        buf = io.StringIO()
        with redirect_stdout(buf):
            captured["cb"](Progress(
                done=3, total=10, written=2048, total_uncompressed=4096,
                net_bytes=1024, total_compressed=2048, speed=512.0, eta=65.0,
                current="a.bin",
            ))
        line = buf.getvalue()
        self.assertIn("3/10", line)
        self.assertIn("2.00 KB", line)
        self.assertIn("512.00 B/s", line)
        self.assertIn("01:05", line)

    def test_nothing_to_do_returns_ok_without_installing(self):
        with Patched(plan=plan_for(need=[])) as p:
            out = run(commands.cmd_install, install_args(apply=True))
            p.install.assert_not_called()
        self.assertIn("没有需要安装的文件", out)

    def test_nothing_to_do_does_not_print_a_dry_run_notice(self):
        """已经没活干了，再说"加 --apply 才真装"是噪音。"""
        with Patched(plan=plan_for(need=[])):
            out = run(commands.cmd_install, install_args(apply=True))
        self.assertNotIn("--apply", out)

    def test_summary_reports_files_bytes_and_requests(self):
        result = Result(done=2, written=4096, net_bytes=2048, requests=17, elapsed=5.0)
        with Patched(result=result):
            out = run(commands.cmd_install, install_args(apply=True))
        self.assertIn("完成：2 个文件", out)
        self.assertIn("4.00 KB", out)
        self.assertIn("请求数 = 17", out)
        self.assertIn("00:05", out)

    def test_stopped_summary_does_not_say_completed(self):
        result = Result(done=1, written=100, net_bytes=50, requests=2,
                        elapsed=3.0, stopped=True)
        with Patched(result=result):
            out = run(commands.cmd_install, install_args(apply=True))
        self.assertIn("已停止", out)
        self.assertNotIn("完成：", out)

    def test_errors_return_partial_not_ok(self):
        """有失败还返回 0，批处理就会把半成品当成装完了。"""
        result = Result(done=1, errors=["a.bin: boom"])
        with Patched(result=result):
            code = commands.cmd_install(install_args(apply=True))
        self.assertEqual(code, EXIT_PARTIAL)

    def test_errors_are_listed(self):
        result = Result(errors=["a.bin: boom", "b.bin: bang"])
        with Patched(result=result):
            out = run(commands.cmd_install, install_args(apply=True))
        self.assertIn("失败 2 个", out)
        self.assertIn("a.bin: boom", out)
        self.assertIn("b.bin: bang", out)

    def test_more_than_twenty_errors_are_summarised(self):
        result = Result(errors=[f"f{i}: boom" for i in range(25)])
        with Patched(result=result):
            out = run(commands.cmd_install, install_args(apply=True))
        self.assertIn("失败 25 个", out)
        self.assertIn("f0: boom", out)
        self.assertIn("f19: boom", out)
        self.assertNotIn("f20: boom", out)
        self.assertIn("另有 5 个", out)

    def test_no_error_section_when_clean(self):
        with Patched(result=Result(done=1)):
            out = run(commands.cmd_install, install_args(apply=True))
        self.assertNotIn("失败", out)

    def test_peak_disk_line_adds_the_single_file_buffer(self):
        plan = plan_for(need=[entry("big.bin", 8192, 4096)])
        result = Result(done=1, written=8192)
        with Patched(plan=plan, result=result):
            out = run(commands.cmd_install, install_args(apply=True))
        # 8192 + 8192 = 16384 -> 16.00 KB
        self.assertIn("16.00 KB", out)
        self.assertIn("未落盘任何压缩包", out)

    def test_pruned_count_is_reported_only_when_nonzero(self):
        with Patched(result=Result(done=1, pruned=3)):
            out = run(commands.cmd_install, install_args(apply=True))
        self.assertIn("已清理多余文件 = 3 个", out)

        with Patched(result=Result(done=1)):
            clean = run(commands.cmd_install, install_args(apply=True))
        self.assertNotIn("已清理", clean)

    def test_prefetch_accounting_is_reported_when_used(self):
        archive = FakeArchive(prefetched=900, wasted=100)
        with Patched(archive=archive, result=Result(done=1)):
            out = run(commands.cmd_install, install_args(apply=True))
        self.assertIn("预读", out)
        self.assertIn("有效 90%", out)

    def test_prefetch_line_is_absent_when_nothing_was_prefetched(self):
        with Patched(archive=FakeArchive(), result=Result(done=1)):
            out = run(commands.cmd_install, install_args(apply=True))
        self.assertNotIn("预读", out)

    def test_rate_limit_wait_is_reported_for_a_limited_run(self):
        limiter = SimpleNamespace(rate=1024.0, waited=125.0)
        archive = FakeArchive(prefetched=10, wasted=0, limiter=limiter)
        with Patched(archive=archive, result=Result(done=1)):
            out = run(commands.cmd_install, install_args(apply=True, limit_rate="1K"))
        self.assertIn("限速", out)
        self.assertIn("累计等待 02:05", out)


class TestCmdProbe(QuietStdout):
    def _probe_archive(self):
        # offset 落在第 10..40 卷之间；compressed 落在 20 000..400 000。
        mid = 20 * 1073741824
        return FakeArchive([
            entry("mid/a.chk", 30_000, 30_000, offset=mid + 1, method=8),
            entry("mid/b.chk", 100_000, 100_000, offset=mid + 2, method=8),
            entry("mid/big.chk", 500_000, 500_000, offset=mid + 3, method=8),
            entry("head/c.chk", 30_000, 30_000, offset=10, method=8),
        ])

    def test_no_candidates_is_an_error(self):
        with Patched(archive=FakeArchive([entry("head/c.chk", 10, 10, offset=0)])):
            code = commands.cmd_probe(SimpleNamespace(count=3))
        self.assertEqual(code, EXIT_ERROR)

    def test_reads_the_requested_number_of_files(self):
        archive = self._probe_archive()
        with Patched(archive=archive):
            commands.cmd_probe(SimpleNamespace(count=1))
        self.assertEqual(len(archive.read_calls), 1)

    def test_picks_the_smallest_candidates(self):
        archive = self._probe_archive()
        with Patched(archive=archive):
            commands.cmd_probe(SimpleNamespace(count=1))
        self.assertEqual(archive.read_calls[0].name, "mid/a.chk")

    def test_skips_files_outside_the_middle_volume_window(self):
        archive = self._probe_archive()
        with Patched(archive=archive):
            commands.cmd_probe(SimpleNamespace(count=10))
        names = {e.name for e in archive.read_calls}
        self.assertNotIn("head/c.chk", names)

    def test_skips_files_outside_the_size_window(self):
        archive = self._probe_archive()
        with Patched(archive=archive):
            commands.cmd_probe(SimpleNamespace(count=10))
        names = {e.name for e in archive.read_calls}
        self.assertNotIn("mid/big.chk", names)

    def test_a_length_mismatch_is_an_error(self):
        """读回来的字节数和清单对不上，说明偏移算错了——必须报错而不是放过。"""
        mid = 20 * 1073741824
        bad = FakeArchive([entry("mid/a.chk", 30_000, 30_000, offset=mid + 1, method=8)])

        def short_read(e):
            bad.read_calls.append(e)
            return b"x" * (e.size - 1)

        bad.read = short_read
        with Patched(archive=bad):
            out_buf = io.StringIO()
            with redirect_stdout(out_buf):
                code = commands.cmd_probe(SimpleNamespace(count=1))
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("FAIL", out_buf.getvalue())

    def test_success_reports_volume_and_sizes(self):
        archive = self._probe_archive()
        with Patched(archive=archive):
            out = run(commands.cmd_probe, SimpleNamespace(count=1))
        self.assertIn("OK", out)
        self.assertIn("卷021", out)
        self.assertIn("CRC32 通过", out)

    def test_reports_the_measured_cost_against_whole_volume_download(self):
        archive = self._probe_archive()
        with Patched(archive=archive):
            out = run(commands.cmd_probe, SimpleNamespace(count=1))
        self.assertIn("若按整卷下载会是", out)
        self.assertIn("实际下载", out)

    def test_range_log_is_printed_when_present(self):
        archive = self._probe_archive()
        with Patched(archive=archive):
            out = run(commands.cmd_probe, SimpleNamespace(count=1))
        # FakeArchive 不填 range_log，所以这里只要求不崩、不误报。
        self.assertNotIn("Range 请求明细", out)

    def test_range_log_lines_are_rendered(self):
        mid = 20 * 1073741824
        archive = FakeArchive([entry("mid/a.chk", 30_000, 30_000, offset=mid + 1, method=8)])

        def fake_open(**kw):
            kw["range_log"].extend([(13, 14088008000, 1048576)])
            return archive

        with mock.patch.object(commands, "open_remote", side_effect=fake_open):
            out = run(commands.cmd_probe, SimpleNamespace(count=1))
        self.assertIn("Range 请求明细", out)
        self.assertIn("卷013", out)


class TestCmdGui(QuietStdout):
    def test_routes_to_the_gui_entry_point(self):
        from efd.ui import entry

        calls = []
        original = entry.main
        entry.main = lambda: calls.append(1)
        try:
            self.assertEqual(commands.cmd_gui(SimpleNamespace()), EXIT_OK)
        finally:
            entry.main = original
        self.assertEqual(calls, [1])

    def test_gui_is_imported_lazily(self):
        """``plan`` / ``install`` 不该为了跑命令行而加载 tkinter。"""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(commands.cmd_gui))
        top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(top_level, [], "cmd_gui 的 import 必须在函数体内")


if __name__ == "__main__":
    unittest.main()
