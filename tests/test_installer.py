"""安装循环测试——完全离线，用夹具里最后一个分卷的真实数据。

覆盖的是真正会写盘的那段代码：流式拷贝、原子替换、进度回调、停机、失败处理。
夹具里只有末卷，所以能实际解压的条目就是「本地头落在末卷内」的那批。
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from efd.archive import load_pack_sizes, open_local
from efd.config import VOLUME_SIZE
from efd.installer import install
from efd.planner import make_plan
from efd.util import safe_join

FIXTURES = Path(__file__).resolve().parent.parent / "data" / "fixtures"
requires_fixture = unittest.skipUnless(
    (FIXTURES / "vol054.bin").exists(), "缺少离线夹具"
)


@requires_fixture
class InstallerCase(unittest.TestCase):
    """共用装置：打开夹具，把计划裁剪成「只能读到末卷条目」。"""

    def setUp(self):
        self.archive = open_local(
            str(FIXTURES), load_pack_sizes(str(FIXTURES / "pack_sizes.json"))
        )
        self.addCleanup(self.archive.close)
        self.target = tempfile.mkdtemp(prefix="efd_install_")
        self.addCleanup(self._rmtree, self.target)

        # 只有本地头落在末卷内的条目才读得出来。
        self.readable = [
            e for e in self.archive.files() if e.offset >= 53 * VOLUME_SIZE
        ]

    @staticmethod
    def _rmtree(path: str) -> None:
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    def make_limited_plan(self, **kwargs):
        plan = make_plan(self.archive, self.target, **kwargs)
        readable_names = {e.name for e in self.readable}
        plan.need = [e for e in plan.need if e.name in readable_names]
        plan.already = [e for e in plan.already if e.name in readable_names]
        return plan


class TestSuccessfulInstall(InstallerCase):
    def test_writes_every_file_with_correct_size(self):
        plan = self.make_limited_plan()
        self.assertGreater(len(plan.need), 0)

        result = install(self.archive, plan)

        self.assertTrue(result.ok, msg=result.errors)
        self.assertEqual(result.done, len(plan.need))
        self.assertEqual(result.written, plan.need_uncompressed)

        for entry in plan.need:
            dest = safe_join(self.target, entry.name)
            self.assertTrue(Path(dest).exists(), msg=entry.name)
            self.assertEqual(Path(dest).stat().st_size, entry.size, msg=entry.name)

    def test_content_matches_archive(self):
        """写出来的字节必须与从归档直接读出来的完全一致。"""
        plan = self.make_limited_plan()
        install(self.archive, plan)
        for entry in plan.need:
            expected = self.archive.read(entry)
            self.assertEqual(Path(safe_join(self.target, entry.name)).read_bytes(),
                             expected, msg=entry.name)

    def test_no_part_files_left_behind(self):
        plan = self.make_limited_plan()
        install(self.archive, plan)
        leftovers = list(Path(self.target).rglob("*.part"))
        self.assertEqual(leftovers, [], f"残留临时文件: {leftovers}")

    def test_creates_nested_directories(self):
        plan = self.make_limited_plan()
        install(self.archive, plan)
        deep = [e for e in plan.need if e.name.count("/") >= 2]
        self.assertTrue(deep, "夹具里应当有嵌套路径的条目")
        for entry in deep:
            self.assertTrue(Path(safe_join(self.target, entry.name)).parent.is_dir())

    def test_second_run_skips_everything(self):
        """装完立刻重跑：不该再需要任何文件。"""
        install(self.archive, self.make_limited_plan())
        again = self.make_limited_plan()
        self.assertEqual(again.need_count, 0)
        self.assertGreater(again.already_count, 0)


class TestProgress(InstallerCase):
    def test_callback_receives_progress(self):
        plan = self.make_limited_plan()
        seen = []
        result = install(self.archive, plan, on_progress=seen.append, interval=0.0)

        self.assertTrue(seen, "进度回调从未被调用")
        last = seen[-1]
        self.assertEqual(last.done, result.done)
        self.assertEqual(last.written, result.written)
        self.assertEqual(last.total, len(plan.need))

    def test_fraction_is_within_range(self):
        plan = self.make_limited_plan()
        seen = []
        install(self.archive, plan, on_progress=seen.append, interval=0.0)
        for p in seen:
            self.assertGreaterEqual(p.fraction, 0.0)
            self.assertLessEqual(p.fraction, 1.0)

    def test_no_duplicate_final_report(self):
        """收尾那次 force 播报不能在「刚好报过」时重复一次。"""
        plan = self.make_limited_plan()
        seen = []
        install(self.archive, plan, on_progress=seen.append, interval=0.0)
        pairs = [(p.done, p.written) for p in seen]
        self.assertEqual(len(pairs), len(set(pairs)), f"有重复播报: {pairs}")

    def test_callback_exception_does_not_break_install(self):
        def boom(_progress):
            raise RuntimeError("回调故意炸")

        plan = self.make_limited_plan()
        result = install(self.archive, plan, on_progress=boom, interval=0.0)
        self.assertTrue(result.ok, msg=result.errors)
        self.assertEqual(result.done, len(plan.need))


class TestStop(InstallerCase):
    def test_preset_stop_event_stops_immediately(self):
        plan = self.make_limited_plan()
        event = threading.Event()
        event.set()

        result = install(self.archive, plan, stop_event=event)

        self.assertTrue(result.stopped)
        self.assertFalse(result.ok)
        self.assertEqual(result.done, 0)
        self.assertEqual(list(Path(self.target).rglob("*")), [])

    def test_stop_midway_leaves_no_partial_file(self):
        plan = self.make_limited_plan()
        self.assertGreaterEqual(len(plan.need), 2)

        event = threading.Event()

        def on_progress(_progress):
            event.set()  # 装完第一个文件就喊停

        result = install(self.archive, plan, on_progress=on_progress,
                         stop_event=event, interval=0.0)

        self.assertTrue(result.stopped)
        self.assertEqual(result.done, 1)
        self.assertEqual(list(Path(self.target).rglob("*.part")), [])
        # 已完成的那一个必须完整，不能是半截
        done_entry = plan.need[0]
        self.assertEqual(Path(safe_join(self.target, done_entry.name)).stat().st_size,
                         done_entry.size)


class TestFailureHandling(InstallerCase):
    def _plan_with_unreadable(self):
        """故意混入一个数据不在本地分卷里的条目。"""
        plan = make_plan(self.archive, self.target)
        readable = {e.name for e in self.readable}
        unreadable = [e for e in plan.need if e.name not in readable][:1]
        self.assertTrue(unreadable)
        good = [e for e in plan.need if e.name in readable][:2]
        plan.need = unreadable + good
        return plan

    def test_failure_is_reported_and_install_aborts(self):
        plan = self._plan_with_unreadable()
        result = install(self.archive, plan)

        self.assertFalse(result.ok)
        self.assertEqual(len(result.errors), 1)
        self.assertIn(plan.need[0].name, result.errors[0])
        self.assertEqual(result.done, 0)

    def test_failure_leaves_no_part_file(self):
        plan = self._plan_with_unreadable()
        install(self.archive, plan)
        self.assertEqual(list(Path(self.target).rglob("*.part")), [])

    def test_keep_going_skips_the_bad_file(self):
        plan = self._plan_with_unreadable()
        result = install(self.archive, plan, keep_going=True)

        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.done, 2)  # 两个好文件都装上了
        self.assertFalse(result.ok)  # 但有错，整体不算成功


class TestPrune(InstallerCase):
    def test_prune_removes_stale_files(self):
        stale = Path(self.target) / "leftover" / "old.chk"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"stale")

        plan = self.make_limited_plan(detect_stale=True)
        self.assertIn("leftover/old.chk", plan.stale)

        result = install(self.archive, plan, prune=True)

        self.assertEqual(result.pruned, 1)
        self.assertFalse(stale.exists())

    def test_no_prune_keeps_stale_files(self):
        stale = Path(self.target) / "keepme.chk"
        stale.write_bytes(b"stale")

        plan = self.make_limited_plan(detect_stale=True)
        install(self.archive, plan, prune=False)

        self.assertTrue(stale.exists())

    def test_prune_spares_hot_updated_vfs_files(self):
        """通道 B 往 VFS 下加的文件不在通道 A 清单里，但它们不是陈旧文件。

        少了这条排除，``--prune`` 会把热更新下来的资源当垃圾删掉。
        """
        hot = (Path(self.target) / "Endfield_Data" / "StreamingAssets"
               / "VFS" / "AABBCCDD" / "EEFF0011.chk")
        hot.parent.mkdir(parents=True, exist_ok=True)
        hot.write_bytes(b"hot-updated resource")

        plan = self.make_limited_plan(detect_stale=True)
        self.assertEqual(plan.stale, [])

        result = install(self.archive, plan, prune=True)
        self.assertEqual(result.pruned, 0)
        self.assertTrue(hot.exists())

    def test_prune_exclusion_stops_at_the_vfs_subtree(self):
        """排除只覆盖 VFS 子树：VFS 之外的陈旧文件照删不误。"""
        hot = (Path(self.target) / "Endfield_Data" / "StreamingAssets"
               / "VFS" / "keepme.chk")
        hot.parent.mkdir(parents=True, exist_ok=True)
        hot.write_bytes(b"keep")
        junk = Path(self.target) / "Endfield_Data" / "leftover.chk"
        junk.write_bytes(b"junk")

        plan = self.make_limited_plan(detect_stale=True)
        self.assertEqual(plan.stale, ["Endfield_Data/leftover.chk"])

        result = install(self.archive, plan, prune=True)
        self.assertEqual(result.pruned, 1)
        self.assertTrue(hot.exists())
        self.assertFalse(junk.exists())


if __name__ == "__main__":
    unittest.main()
