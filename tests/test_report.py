"""``efd/cli/report.py`` 的渲染测试。

这一层是纯只读的：只接收已经算好的 archive/plan，不发请求也不写盘。它因此
既最容易测，也最容易**漏测**——在补上这组测试之前它的覆盖率是 14.8%，
三个 print 函数几乎一行都没跑到。

重点不是"有没有输出"，而是**数字有没有印错**：这是用户唯一会读的东西，
把一个数字印错（比如把目录条目算进解压总量、把峰值公式漏掉缓冲）不会
抛异常，只会让人据此做错决定。所以断言全部落在具体数字上，并且用
``_fields()`` 把标签到值的对应关系解析回来，而不是匹配整行文本——
整行匹配会在调整列宽时全部假失败。
"""

from __future__ import annotations

import io
import re
import unittest
from contextlib import redirect_stdout

from efd.cli.report import print_distribution, print_header, print_plan
from efd.core.archive import Entry
from efd.core.planner import Plan

_LINE = re.compile(r"^(?P<label>.+?)\s*=\s*(?P<value>.+?)\s*$")
_ANNOTATION = re.compile(r"\s*<-.*$")


def _fields(text: str) -> dict[str, str]:
    """把 ``标签 = 值`` 的渲染结果解析回字典。

    值里跟在 ``<-`` 后面的注释会被丢掉，因为它只是给人看的旁注。
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if match:
            label = match.group("label").strip()
            out[label] = _ANNOTATION.sub("", match.group("value")).strip()
    return out


def _render(func, *args) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        func(*args)
    return buf.getvalue()


def entry(name: str, size: int, compressed: int = 0, *, is_dir: bool = False,
          offset: int = 0, method: int = 8) -> Entry:
    return Entry(name=name, size=size, compressed=compressed, crc=0,
                 offset=offset, method=method, is_dir=is_dir)


class FakeArchive:
    """只提供 report 层真正会读的那几个属性。"""

    def __init__(self, files, dirs=(), *, version="1.5.3", total_bytes=0,
                 net_bytes=0, volumes=3):
        self.version = version
        self.total_bytes = total_bytes
        self.net_bytes = net_bytes
        self.volumes = list(range(volumes))
        self.entries = list(files) + list(dirs)
        self._files = list(files)
        self._dirs = list(dirs)

    def files(self):
        return list(self._files)

    def dirs(self):
        return list(self._dirs)


class TestPrintHeader(unittest.TestCase):
    def _header(self, archive) -> dict[str, str]:
        return _fields(_render(print_header, archive))

    def test_prints_version_and_volume_count(self):
        got = self._header(FakeArchive([entry("a.txt", 10)], version="9.9.9", volumes=54))
        self.assertEqual(got["游戏版本"], "9.9.9")
        self.assertIn("54 分卷", got["归档逻辑长度"])

    def test_counts_entries_and_splits_files_from_dirs(self):
        archive = FakeArchive(
            [entry("a.txt", 10), entry("b.txt", 20)],
            [entry("d/", 0, is_dir=True)],
        )
        got = self._header(archive)
        self.assertEqual(got["清单条目"], "3")
        self.assertIn("文件条目", got)
        self.assertIn("2", got["文件条目"])
        self.assertIn("目录条目 1", got["文件条目"])

    def test_totals_exclude_directory_entries(self):
        """目录条目在档案里也带一个 size，但它不是内容，绝不能算进总量。

        真实数据里 91 个目录条目的 size 全是 0，所以这个 bug 只会在
        "某个目录条目恰好有非零 size" 时显形——那正是最不该赌的形状。
        """
        archive = FakeArchive(
            [entry("a.txt", 1000, 400), entry("b.txt", 2000, 800)],
            [entry("d/", 999_999, is_dir=True)],
        )
        got = self._header(archive)
        self.assertEqual(got["解压后总量"], "2.93 KB")
        self.assertEqual(got["压缩后总量"], "1.17 KB")

    def test_manifest_cost_is_labelled_as_one_request(self):
        archive = FakeArchive([entry("a.txt", 10)], net_bytes=414_909)
        got = self._header(archive)
        self.assertEqual(got["清单下载成本"], "405.18 KB")
        self.assertIn("一次 Range 请求", _render(print_header, archive))

    def test_empty_archive_does_not_crash(self):
        got = self._header(FakeArchive([]))
        self.assertEqual(got["解压后总量"], "0.00 B")
        self.assertEqual(got["文件条目"].split("(")[0].strip(), "0")


class TestPrintDistribution(unittest.TestCase):
    def test_groups_by_top_level_directory(self):
        archive = FakeArchive([
            entry("Endfield_Data/a.bin", 100),
            entry("Endfield_Data/b.bin", 200),
            entry("Launcher/c.bin", 50),
        ])
        text = _render(print_distribution, archive)
        self.assertIn("顶层分布", text)
        self.assertIn("Endfield_Data", text)
        # 2 个文件 / 300 B
        self.assertRegex(text, r"Endfield_Data\s+2 个")

    def test_top_level_is_sorted_by_size_descending(self):
        archive = FakeArchive([
            entry("small/a.bin", 1),
            entry("huge/b.bin", 10_000),
        ])
        text = _render(print_distribution, archive)
        self.assertLess(text.index("huge"), text.index("small"),
                        "体积大的顶层目录必须排在前面")

    def test_lists_only_the_largest_eight(self):
        archive = FakeArchive([entry(f"d{i}/f.bin", i + 1) for i in range(12)])
        text = _render(print_distribution, archive)
        names = re.findall(r"d\d+/f\.bin", text.split("最大的 8 个文件:")[1])
        self.assertEqual(len(names), 8)

    def test_vfs_block_distribution_uses_the_component_after_vfs(self):
        archive = FakeArchive([
            entry("Endfield_Data/StreamingAssets/VFS/AAAA1111/x.chk", 100),
            entry("Endfield_Data/StreamingAssets/VFS/AAAA1111/y.chk", 100),
            entry("Endfield_Data/StreamingAssets/VFS/BBBB2222/z.chk", 50),
        ])
        text = _render(print_distribution, archive)
        self.assertIn("VFS 块分布", text)
        self.assertRegex(text, r"AAAA1111\s+2 个")
        self.assertRegex(text, r"BBBB2222\s+1 个")

    def test_no_vfs_section_when_there_is_no_vfs(self):
        text = _render(print_distribution, FakeArchive([entry("plain/a.bin", 5)]))
        self.assertNotIn("VFS 块分布", text)

    def test_a_path_ending_in_vfs_does_not_crash(self):
        """回归：``VFS`` 正好是路径最后一段时，取下一段会越界。

        真实数据里没有这种条目（已用夹具的 1601 个文件核对过），但这条路径
        每次 ``plan`` / ``install`` 都会走到，越界会在规划之前就打断整个命令。
        """
        archive = FakeArchive([
            entry("Endfield_Data/StreamingAssets/VFS", 10),
            entry("Endfield_Data/StreamingAssets/VFS/AAAA1111/x.chk", 20),
        ])
        text = _render(print_distribution, archive)
        self.assertIn("VFS 块分布", text)
        self.assertRegex(text, r"AAAA1111\s+1 个")

    def test_a_bare_vfs_component_is_ignored_everywhere(self):
        archive = FakeArchive([entry("VFS", 10)])
        text = _render(print_distribution, archive)
        self.assertNotIn("VFS 块分布", text)

    def test_deep_nesting_still_groups_by_the_first_vfs(self):
        archive = FakeArchive([
            entry("a/VFS/ONE/VFS/TWO/x.chk", 1),
        ])
        text = _render(print_distribution, archive)
        self.assertRegex(text, r"ONE\s+1 个")


class TestPrintPlan(unittest.TestCase):
    def _plan(self, **kw) -> Plan:
        base = dict(
            version="1.5.3",
            target=r"D:\Games\Endfield",
            archive_bytes=50_000,
            manifest_bytes=414_909,
            entries_total=3,
            already=[entry("kept.bin", 1000)],
            need=[entry("a.bin", 2000, 800), entry("b.bin", 3000, 900)],
        )
        base.update(kw)
        return Plan(**base)

    def _rendered(self, plan) -> dict[str, str]:
        return _fields(_render(print_plan, plan))

    def test_prints_target_and_counts(self):
        got = self._rendered(self._plan())
        self.assertEqual(got["目标目录"], r"D:\Games\Endfield")
        self.assertIn("1 个", got["已存在跳过"])
        self.assertEqual(got["本次安装"], "2 个文件")

    def test_reports_the_real_download_cost_not_the_uncompressed_size(self):
        """要下载的是**压缩**字节。把解压后体积当成下载量会高估三倍。"""
        got = self._rendered(self._plan())
        self.assertEqual(got["需下载(压缩)"], "1.66 KB")
        self.assertEqual(got["解压后"], "4.88 KB")

    def test_biggest_single_file_is_shown_as_the_resume_granularity(self):
        got = self._rendered(self._plan())
        self.assertEqual(got["最大单文件"], "2.93 KB")
        self.assertIn("缓冲/续传粒度", _render(print_plan, self._plan()))

    def test_peak_math_includes_the_single_file_buffer(self):
        plan = self._plan()
        got = self._rendered(plan)
        # final = already(1000) + need(5000) = 6000；峰值 = 6000 + biggest(3000)
        self.assertEqual(got["本方案"], "最终 5.86 KB + 单文件缓冲 2.93 KB = 8.79 KB")
        # 官方 = 压缩包(50000) + 解压产物(6000)
        self.assertEqual(got["官方"], "压缩包 48.83 KB + 解压产物 5.86 KB = 54.69 KB")
        self.assertEqual(got["节省"], "45.90 KB")

    def test_saving_equals_the_difference_of_the_two_peaks(self):
        plan = self._plan()
        self.assertEqual(plan.saving, plan.peak_official - plan.peak_ours)

    def test_absent_sections_are_omitted(self):
        text = _render(print_plan, self._plan())
        self.assertNotIn("排除", text)
        self.assertNotIn("路径不可信", text)
        self.assertNotIn("本地多余", text)

    def test_excluded_branch_shows_count_and_size(self):
        plan = self._plan(excluded=[entry("x.bin", 2048), entry("y.bin", 0)])
        text = _render(print_plan, plan)
        self.assertIn("排除", text)
        self.assertRegex(text, r"排除\s+= 2 个\s+2\.00 KB")

    def test_unsafe_branch_warns_and_points_at_json(self):
        plan = self._plan(unsafe=["../escape", "C:/abs"])
        text = _render(print_plan, plan)
        self.assertIn("路径不可信", text)
        self.assertIn("2 个", text)
        self.assertIn("--json", text)

    def test_stale_branch_shows_the_count(self):
        plan = self._plan(stale=["old.bin", "gone.bin"])
        text = _render(print_plan, plan)
        self.assertIn("本地多余(陈旧)", text)
        self.assertIn("2 个", text)

    def test_empty_plan_prints_zeros_not_an_error(self):
        plan = self._plan(already=[], need=[])
        got = self._rendered(plan)
        self.assertEqual(got["本次安装"], "0 个文件")
        self.assertEqual(got["最大单文件"], "0.00 B")
        self.assertEqual(got["本方案"], "最终 0.00 B + 单文件缓冲 0.00 B = 0.00 B")
        # 没有要装的东西时，省下的就是整个压缩包——本方案根本不下载它。
        self.assertEqual(got["节省"], "48.83 KB")


if __name__ == "__main__":
    unittest.main()
