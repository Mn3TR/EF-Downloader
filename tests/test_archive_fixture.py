"""离线夹具回归测试。

夹具：``data/fixtures/vol054.bin``（最后一卷，6.7 MB，中央目录完整落在其中）
      ``data/fixtures/pack_sizes.json``（54 卷的真实尺寸，由 Seed 接口导出）

只有末卷也能解析出完整清单——这正是 :class:`efd.volumes.MissingVolume`
存在的原因：它不提供数据，但**提供正确的尺寸**，从而让所有偏移量成立。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from efd.archive import open_local, load_pack_sizes
from efd.config import VOLUME_SIZE
from efd.planner import make_plan
from efd.util import safe_join

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "data" / "fixtures"
VOL054 = FIXTURES / "vol054.bin"
PACK_SIZES = FIXTURES / "pack_sizes.json"

# 由夹具实测得到，作为回归基线钉死。
# 任何一条变动都意味着中央目录解析出了问题，必须查清楚再改。
ENTRIES = 1692
FILES = 1601  # 含 1 个 0 字节空文件，见 TestEmptyFileRegression
DIRS = 91
TOTAL_BYTES = 56_915_023_037
UNCOMPRESSED = 62_233_634_670
COMPRESSED = 56_914_606_683

# 旧实现用 `if file_size or compress_size` 判断"是不是文件"，
# 于是这个真实的空文件被当成目录丢掉了，从来没被创建过。
EMPTY_FILE = "Endfield_Data/Plugins/x86_64/TQM64/dump/space.txt"

requires_fixture = unittest.skipUnless(
    VOL054.exists() and PACK_SIZES.exists(),
    f"缺少夹具（需要 {VOL054} 与 {PACK_SIZES}）",
)


@requires_fixture
class TestOfflineArchive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sizes = load_pack_sizes(str(PACK_SIZES))
        cls.archive = open_local(str(FIXTURES), cls.sizes, version="fixture")

    @classmethod
    def tearDownClass(cls):
        cls.archive.close()

    def test_volume_layout(self):
        self.assertEqual(len(self.sizes), 54)
        self.assertTrue(all(s == VOLUME_SIZE for s in self.sizes[:-1]))
        self.assertEqual(sum(self.sizes), TOTAL_BYTES)

    def test_total_bytes_reconstructed_from_sizes(self):
        """只有末卷在本地，总长度仍必须等于 54 卷之和。"""
        self.assertEqual(self.archive.total_bytes, TOTAL_BYTES)

    def test_entry_counts(self):
        self.assertEqual(len(self.archive.entries), ENTRIES)
        self.assertEqual(len(self.archive.files()), FILES)
        self.assertEqual(len(self.archive.dirs()), DIRS)

    def test_dir_flag_matches_trailing_slash(self):
        for entry in self.archive.entries:
            self.assertEqual(entry.is_dir, entry.name.endswith("/"), msg=entry.name)

    def test_totals(self):
        files = self.archive.files()
        self.assertEqual(sum(e.size for e in files), UNCOMPRESSED)
        self.assertEqual(sum(e.compressed for e in files), COMPRESSED)

    def test_offsets_within_archive(self):
        for entry in self.archive.entries:
            self.assertLess(entry.offset, TOTAL_BYTES, msg=entry.name)

    def test_local_volumes_report_no_network(self):
        self.assertEqual(self.archive.net_bytes, 0)
        self.assertEqual(self.archive.net_requests, 0)


@requires_fixture
class TestReadFromLastVolume(unittest.TestCase):
    """真正解压若干条目：覆盖 ConcatReader -> zipfile -> inflate -> CRC32 全链路。"""

    @classmethod
    def setUpClass(cls):
        cls.archive = open_local(str(FIXTURES), load_pack_sizes(str(PACK_SIZES)),
                                 version="fixture")
        # 本地头落在末卷内的条目，其数据必然也在末卷内。
        cls.in_vol54 = [
            e for e in cls.archive.files() if e.offset >= 53 * VOLUME_SIZE
        ]

    @classmethod
    def tearDownClass(cls):
        cls.archive.close()

    def test_there_are_entries_in_last_volume(self):
        self.assertGreater(len(self.in_vol54), 0)

    def test_read_all_last_volume_entries(self):
        for entry in self.in_vol54:
            with self.subTest(entry=entry.name):
                # zipfile 会在读完后校验 CRC32，不一致会抛 BadZipFile
                data = self.archive.read(entry)
                self.assertEqual(len(data), entry.size)

    def test_streaming_read_matches_read(self):
        entry = self.in_vol54[0]
        with self.archive.open(entry) as f:
            streamed = f.read()
        self.assertEqual(streamed, self.archive.read(entry))

    def test_reading_absent_volume_raises(self):
        """数据在其他卷里的条目应当明确报错，而不是静默给出垃圾。"""
        elsewhere = [e for e in self.archive.files() if e.offset < 53 * VOLUME_SIZE]
        self.assertTrue(elsewhere)
        with self.assertRaises(RuntimeError):
            self.archive.read(elsewhere[0])


@requires_fixture
class TestEmptyFileRegression(unittest.TestCase):
    """空文件必须被当作文件，而不是目录。"""

    @classmethod
    def setUpClass(cls):
        cls.archive = open_local(str(FIXTURES), load_pack_sizes(str(PACK_SIZES)),
                                 version="fixture")

    @classmethod
    def tearDownClass(cls):
        cls.archive.close()

    def test_empty_file_is_a_file(self):
        found = [e for e in self.archive.files() if e.name == EMPTY_FILE]
        self.assertEqual(len(found), 1, "空文件必须出现在 files() 里")
        entry = found[0]
        self.assertEqual(entry.size, 0)
        self.assertEqual(entry.compressed, 0)
        self.assertFalse(entry.is_dir)

    def test_is_only_zero_length_file(self):
        zero = [e for e in self.archive.files() if e.size == 0]
        self.assertEqual([e.name for e in zero], [EMPTY_FILE])

    def test_old_heuristic_would_have_dropped_it(self):
        """记录旧实现的判据，防止有人"优化"回去。"""
        by_heuristic = [e for e in self.archive.entries if e.size or e.compressed]
        self.assertEqual(len(by_heuristic), 1600)  # 少一个
        self.assertNotIn(EMPTY_FILE, {e.name for e in by_heuristic})


@requires_fixture
class TestPlannerAgainstFixture(unittest.TestCase):
    """规划器在真实清单上的行为（目标目录为空）。"""

    def setUp(self):
        self.archive = open_local(str(FIXTURES), load_pack_sizes(str(PACK_SIZES)),
                                  version="fixture")
        self.tmp = Path(__file__).resolve().parent / "_tmp_target"
        self.tmp.mkdir(exist_ok=True)

    def tearDown(self):
        self.archive.close()
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def test_empty_target_wants_everything(self):
        plan = make_plan(self.archive, str(self.tmp))
        self.assertEqual(plan.need_count, FILES)
        self.assertEqual(plan.already_count, 0)
        self.assertEqual(plan.need_uncompressed, UNCOMPRESSED)
        self.assertEqual(plan.peak_ours, UNCOMPRESSED + plan.biggest)

    def test_biggest_is_the_1_5gb_chunk(self):
        """最大单文件决定了内存缓冲与续传粒度，必须钉死。

        实测 1,610,306,268 B（显示为 1.50 GB）。这是**量出来的**，
        不是把 "1.50 GiB" 换算回去——两者差 3 MB。
        """
        plan = make_plan(self.archive, str(self.tmp))
        self.assertEqual(plan.biggest, 1_610_306_268)
        self.assertLess(plan.biggest, 1.5 * 1024**3)
        self.assertGreater(plan.biggest, 1.49 * 1024**3)

    def test_peak_saving_matches_documented_math(self):
        plan = make_plan(self.archive, str(self.tmp))
        self.assertEqual(plan.peak_official, TOTAL_BYTES + UNCOMPRESSED)
        self.assertEqual(
            plan.saving, plan.peak_official - plan.peak_ours
        )
        # 官方方式需要 110 GB 以上，本方案 60 GB 出头
        self.assertGreater(plan.peak_official, 110 * 1024**3)
        self.assertLess(plan.peak_ours, 64 * 1024**3)

    def test_order_is_by_offset(self):
        plan = make_plan(self.archive, str(self.tmp))
        offsets = [e.offset for e in plan.need]
        self.assertEqual(offsets, sorted(offsets), "必须按归档顺序，保证零回退读")

    def test_exclude_streaming(self):
        from efd.config import EXCLUDE_STREAMING
        plan = make_plan(self.archive, str(self.tmp),
                         exclude_prefixes=EXCLUDE_STREAMING)
        self.assertGreater(len(plan.excluded), 0)
        self.assertLess(plan.need_count, FILES)
        self.assertLess(plan.need_uncompressed, 2 * 1024**3)

    def test_limit(self):
        plan = make_plan(self.archive, str(self.tmp), limit=5)
        self.assertEqual(plan.need_count, 5)

    def test_resume_skips_existing(self):
        """造一个尺寸正确的文件，它就该被跳过。"""
        target = self.archive.files()[0]
        dest = safe_join(str(self.tmp), target.name)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"\0" * target.size)

        plan = make_plan(self.archive, str(self.tmp))
        self.assertEqual(plan.already_count, 1)
        self.assertEqual(plan.need_count, FILES - 1)
        self.assertNotIn(target.name, {e.name for e in plan.need})

    def test_resume_ignores_wrong_size(self):
        target = self.archive.files()[0]
        dest = safe_join(str(self.tmp), target.name)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"\0" * 7)  # 尺寸不对

        plan = make_plan(self.archive, str(self.tmp))
        self.assertEqual(plan.already_count, 0)
        self.assertIn(target.name, {e.name for e in plan.need})

    def test_plan_is_json_serialisable_and_complete(self):
        import json
        plan = make_plan(self.archive, str(self.tmp))
        data = plan.to_dict()
        text = json.dumps(data, ensure_ascii=False)  # 不抛异常
        self.assertGreater(len(text), 0)
        # 必须是全量，不能截断
        self.assertEqual(len(data["need"]), plan.need_count)


if __name__ == "__main__":
    unittest.main()
