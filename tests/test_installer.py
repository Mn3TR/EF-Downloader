"""安装循环测试——完全离线，用夹具里最后一个分卷的真实数据。

覆盖的是真正会写盘的那段代码：流式拷贝、原子替换、进度回调、停机、失败处理。
夹具里只有末卷，所以能实际解压的条目就是「本地头落在末卷内」的那批。
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from efd.core import journal
from efd.core.archive import load_pack_sizes, open_local
from efd.core.config import VOLUME_SIZE
from efd.core.installer import install
from efd.core.planner import make_plan
from efd.core.util import safe_join

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

        # 安装日志是 ``--prune`` 的判据，装一次就会写。测试绝不能碰用户真实的
        # ``%LOCALAPPDATA%\EFD\journal.json``——跟 test_settings 打桩 config_path 一个道理。
        self.state = Path(tempfile.mkdtemp(prefix="efd_journal_"))
        self.addCleanup(self._rmtree, str(self.state))
        patcher = mock.patch.object(
            journal, "state_path", lambda: self.state / journal.FILE_NAME
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        # 只有本地头落在末卷内的条目才读得出来。
        self.readable = [
            e for e in self.archive.files() if e.offset >= 53 * VOLUME_SIZE
        ]

    def write(self, rel: str, data: bytes) -> Path:
        """在目标目录里造一个文件，返回它的路径。"""
        path = Path(self.target) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def remember(self, **sizes: int) -> None:
        """假装这些文件是本工具以前装的——``--prune`` 只认日志记过的。"""
        journal.update(self.target, added=list(sizes.items()))

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

    def test_stop_takes_effect_inside_the_current_file(self):
        """停机必须在**当前文件内部**生效，不能等它写完。

        只在文件与文件之间查一次 ``stop_event`` 的话，按了「停止」之后要等
        整个文件下完才生效——而最大的单文件有 2 GB 以上，界面十几分钟没反应，
        用户只会认为程序卡死了（然后去关窗口，留下一个巨大的 ``.part``）。
        """
        from unittest import mock

        from efd.core import installer as inst

        plan = self.make_limited_plan()
        self.assertGreaterEqual(len(plan.need), 2)

        event = threading.Event()

        def on_progress(progress):
            if progress.done == 0:  # 第一个文件还在拷，就喊停
                event.set()

        with mock.patch.object(inst, "COPY_CHUNK", 1 << 16):
            result = install(self.archive, plan, on_progress=on_progress,
                             stop_event=event, interval=0.0)

        self.assertTrue(result.stopped)
        self.assertEqual(result.done, 0, "半截文件不该被算作已完成")
        self.assertEqual(result.written, 0)
        # 半截文件必须删掉，绝不能 promote 成正式文件
        self.assertEqual(list(Path(self.target).rglob("*.part")), [])
        self.assertEqual([p for p in Path(self.target).rglob("*") if p.is_file()], [],
                         "中止的那个文件不该在盘上留下任何东西")

    def test_progress_advances_inside_a_single_file(self):
        """大文件拷贝途中也要报进度，否则界面看起来是冻住的。

        只在文件边界回调的话，一个 2 GB 的文件要下十几分钟，这期间进度条、
        速度、当前文件名全都不动——用户没有任何办法判断它是不是还活着。
        """
        from unittest import mock

        from efd.core import installer as inst

        plan = self.make_limited_plan()
        seen = []
        with mock.patch.object(inst, "COPY_CHUNK", 1 << 16):
            install(self.archive, plan, on_progress=seen.append, interval=0.0)

        # done == 0 的那些回调，全都发生在第一个文件还没拷完的时候
        mid_file = [p for p in seen if p.done == 0]
        self.assertGreaterEqual(len(mid_file), 2,
                                f"第一个文件内部只播报了 {len(mid_file)} 次进度")
        written = [p.written for p in mid_file]
        self.assertEqual(written, sorted(written), "文件内部的进度没有单调增长")
        self.assertGreater(written[-1], 0)

    def test_speed_never_goes_negative_after_a_stop(self):
        """中止后停在界面上的那一帧，速度不能是负的。

        拷贝途中播报的是「已拷字节」，收尾那次按「已落盘字节」算；半截文件
        一被丢弃，增量就成了负数。实测在真机界面上出现过 ``-9.18 GB/s``。
        """
        from unittest import mock

        from efd.core import installer as inst

        plan = self.make_limited_plan()
        event = threading.Event()
        seen = []

        def on_progress(progress):
            seen.append(progress)
            if progress.done == 0:
                event.set()

        with mock.patch.object(inst, "COPY_CHUNK", 1 << 16):
            result = install(self.archive, plan, on_progress=on_progress,
                             stop_event=event, interval=0.0)

        self.assertTrue(result.stopped)
        self.assertTrue(seen, "进度回调从未被调用")
        for p in seen:
            self.assertGreaterEqual(p.speed, 0.0, f"出现负速度: {p.speed}")
        # 最后一帧说的是「已落盘多少」，和中止摘要口径一致
        self.assertEqual(seen[-1].written, result.written)

    def test_stop_midway_leaves_no_partial_file(self):
        plan = self.make_limited_plan()
        self.assertGreaterEqual(len(plan.need), 2)

        event = threading.Event()

        def on_progress(progress):
            # 注意要等 done 涨到 1：拷贝途中也会回调，直接 set 的话
            # 会变成「文件内部中止」，那是上面那个测试的事。
            if progress.done >= 1:
                event.set()

        result = install(self.archive, plan, on_progress=on_progress,
                         stop_event=event, interval=0.0)

        self.assertTrue(result.stopped)
        self.assertEqual(result.done, 1)
        self.assertEqual(list(Path(self.target).rglob("*.part")), [])
        # 已完成的那一个必须完整，不能是半截
        done_entry = plan.need[0]
        self.assertEqual(Path(safe_join(self.target, done_entry.name)).stat().st_size,
                         done_entry.size)

    def test_orphan_part_from_a_killed_run_is_swept(self):
        """上次被强杀留下的 ``.part`` 要清掉。

        关窗口时 worker 是 daemon 线程，进程立刻退出，正在写的文件就留在盘上了。
        我们从不续写半截文件，所以它只有占地方一个作用。
        """
        plan = self.make_limited_plan()
        entry = plan.need[0]
        orphan = Path(safe_join(self.target, entry.name) + ".part")
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"x" * 4096)

        install(self.archive, plan)

        self.assertFalse(orphan.exists(), "上一次残留的 .part 没有被清掉")
        self.assertEqual(list(Path(self.target).rglob("*.part")), [])


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
        """日志记过、尺寸没变、清单里又没了——这才是「陈旧」，可以回收。"""
        stale = self.write("leftover/old.chk", b"stale")
        self.remember(**{"leftover/old.chk": 5})

        plan = self.make_limited_plan(detect_stale=True)
        self.assertIn("leftover/old.chk", plan.stale)

        result = install(self.archive, plan, prune=True)

        self.assertEqual(result.pruned, 1)
        self.assertFalse(stale.exists())

    def test_prune_spares_files_the_journal_never_saw(self):
        """游戏运行期写的文件从没进过日志，所以永远不会被删。

        这条就是当初那个 bug：`mmkv/`、`CrashSightLog/`、`Endfield_Data/Persistent/`
        里的存档和缓存在清单里都没有，早先的判据会把它们当垃圾清掉。
        """
        runtime = self.write("mmkv/login_cache", b"runtime cache")
        persistent = self.write("Endfield_Data/Persistent/index_main.json", b"{}")

        plan = self.make_limited_plan(detect_stale=True)

        self.assertEqual(plan.stale, [])
        self.assertIn("mmkv/login_cache", plan.foreign)
        self.assertIn("Endfield_Data/Persistent/index_main.json", plan.foreign)

        result = install(self.archive, plan, prune=True)

        self.assertEqual(result.pruned, 0)
        self.assertTrue(runtime.exists())
        self.assertTrue(persistent.exists())

    def test_a_logged_file_of_a_different_size_is_left_alone(self):
        """尺寸对不上就说明文件已经易主，不再归本工具处置。"""
        changed = self.write("leftover/old.chk", b"someone else rewrote this")
        self.remember(**{"leftover/old.chk": 5})  # 日志里记的是 5 字节

        plan = self.make_limited_plan(detect_stale=True)

        self.assertEqual(plan.stale, [])
        self.assertIn("leftover/old.chk", plan.foreign)

        result = install(self.archive, plan, prune=True)
        self.assertEqual(result.pruned, 0)
        self.assertTrue(changed.exists())

    def test_install_records_what_it_wrote(self):
        """装完必须把落盘的文件记进日志，否则下次 --prune 无从判断。"""
        plan = self.make_limited_plan()
        self.assertTrue(plan.need, "夹具里应当有可安装的条目")

        install(self.archive, plan)

        logged = journal.load(self.target)
        for entry in plan.need:
            self.assertEqual(logged.get(entry.name), entry.size)

    def test_already_installed_files_are_recorded_too(self):
        """早就装好的文件也要补记进日志。

        真实场景：用户拿旧版本装完 57 GiB，才升级到带安装日志的这一版。
        这些文件的路径和尺寸跟清单分毫不差，``_looks_installed`` 已经认过一遍;
        不补记的话，它们永远是「外来文件」，``--prune`` 再也回收不了旧版本残留——
        而那正是这个功能存在的意义。
        """
        # 先空跑一次记录基线，再"重装"一次：这一次全都已在盘上。
        first = self.make_limited_plan()
        install(self.archive, first)
        self.assertTrue(first.need, "夹具里应当有可安装的条目")

        second = self.make_limited_plan()
        self.assertEqual(second.need, [])
        self.assertEqual(len(second.already), len(first.need))

        # 把日志清空，模拟"日志启用之前就装好了"的目录。
        journal.update(self.target, removed=[e.name for e in first.need])
        self.assertEqual(journal.load(self.target), {})

        install(self.archive, second)

        logged = journal.load(self.target)
        for entry in second.already:
            self.assertEqual(logged.get(entry.name), entry.size)

    def test_prune_forgets_what_it_deleted(self):
        """删掉的文件要从日志里划掉，否则下次还要白跑一遍。"""
        self.write("leftover/old.chk", b"stale")
        self.remember(**{"leftover/old.chk": 5})

        plan = self.make_limited_plan(detect_stale=True)
        install(self.archive, plan, prune=True)

        self.assertNotIn("leftover/old.chk", journal.load(self.target))

    def test_no_prune_keeps_stale_files(self):
        stale = self.write("keepme.chk", b"stale")
        self.remember(**{"keepme.chk": 5})

        plan = self.make_limited_plan(detect_stale=True)
        install(self.archive, plan, prune=False)

        self.assertTrue(stale.exists())

    def test_prune_spares_hot_updated_vfs_files(self):
        """通道 B 往 VFS 下加的文件不在通道 A 清单里，但它们不是陈旧文件。

        少了这条排除，``--prune`` 会把热更新下来的资源当垃圾删掉。
        注意这里**故意也记进日志**——即便记过，VFS 子树也照样排除。
        """
        hot = self.write("Endfield_Data/StreamingAssets/VFS/AABBCCDD/EEFF0011.chk",
                         b"hot-updated resource")
        self.remember(**{"Endfield_Data/StreamingAssets/VFS/AABBCCDD/EEFF0011.chk": 20})

        plan = self.make_limited_plan(detect_stale=True)
        self.assertEqual(plan.stale, [])

        result = install(self.archive, plan, prune=True)
        self.assertEqual(result.pruned, 0)
        self.assertTrue(hot.exists())

    def test_prune_exclusion_stops_at_the_vfs_subtree(self):
        """排除只覆盖 VFS 子树：VFS 之外的陈旧文件照删不误。"""
        hot = self.write("Endfield_Data/StreamingAssets/VFS/keepme.chk", b"keep")
        junk = self.write("Endfield_Data/leftover.chk", b"junk")
        self.remember(**{
            "Endfield_Data/StreamingAssets/VFS/keepme.chk": 4,
            "Endfield_Data/leftover.chk": 4,
        })

        plan = self.make_limited_plan(detect_stale=True)
        self.assertEqual(plan.stale, ["Endfield_Data/leftover.chk"])

        result = install(self.archive, plan, prune=True)
        self.assertEqual(result.pruned, 1)
        self.assertTrue(hot.exists())
        self.assertFalse(junk.exists())


if __name__ == "__main__":
    unittest.main()
