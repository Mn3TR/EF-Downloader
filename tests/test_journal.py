"""安装日志测试——``--prune`` 的唯一判据。

这个模块的价值全在「它敢不敢说某个文件可以删」上，所以用例集中在三件事：
畸形输入不会让它乱认文件、尺寸对不上就不认、以及状态文件不进游戏目录。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from efd.core import journal, settings


class JournalCase(unittest.TestCase):
    """把状态文件钉在临时目录里——绝不能碰用户真实的 ``%LOCALAPPDATA%\\EFD\\``。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="efd_journal_"))
        self.addCleanup(self._rmtree, self.tmp)
        self.path = self.tmp / "sub" / journal.FILE_NAME
        # 留一份真函数，用来断言「没被打桩时它指哪儿」——打桩之后就没法问了。
        self.real_state_path = journal.state_path
        patcher = mock.patch.object(journal, "state_path", lambda: self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.target = r"D:\Games\Endfield"

    @staticmethod
    def _rmtree(path: Path) -> None:
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    def write_raw(self, payload) -> None:
        """直接往状态文件里塞任意内容，模拟被手动改坏的情况。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )


class TestRoundTrip(JournalCase):
    def test_missing_file_reads_as_empty(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(journal.load(self.target), {})

    def test_records_survive_a_reload(self):
        journal.update(self.target, added=[("a.bin", 100), ("sub/b.bin", 200)])
        self.assertEqual(journal.load(self.target), {"a.bin": 100, "sub/b.bin": 200})

    def test_removed_names_are_forgotten(self):
        journal.update(self.target, added=[("a.bin", 100), ("b.bin", 200)])
        journal.update(self.target, removed=["a.bin"])
        self.assertEqual(journal.load(self.target), {"b.bin": 200})

    def test_removing_an_unknown_name_is_harmless(self):
        journal.update(self.target, added=[("a.bin", 100)])
        journal.update(self.target, removed=["nope.bin"])
        self.assertEqual(journal.load(self.target), {"a.bin": 100})

    def test_each_target_is_tracked_separately(self):
        other = r"D:\Games\Endfield-Test"
        journal.update(self.target, added=[("a.bin", 100)])
        journal.update(other, added=[("b.bin", 200)])
        self.assertEqual(journal.load(self.target), {"a.bin": 100})
        self.assertEqual(journal.load(other), {"b.bin": 200})

    def test_target_paths_are_matched_case_insensitively_on_windows(self):
        """同一个目录换个大小写写法，不该被当成两个目标。"""
        journal.update(r"D:\Games\Endfield", added=[("a.bin", 100)])
        self.assertEqual(journal.load(r"d:\games\endfield"), {"a.bin": 100})

    def test_trailing_separator_is_the_same_target(self):
        journal.update(r"D:\Games\Endfield", added=[("a.bin", 100)])
        self.assertEqual(journal.load("D:\\Games\\Endfield\\"), {"a.bin": 100})

    def test_state_file_lives_next_to_the_settings(self):
        """状态文件进 ``%LOCALAPPDATA%\\EFD\\``，**绝不往游戏目录里塞**。

        这条得问**没被打桩的**那个函数——装置里的 ``state_path`` 已经指向临时目录了，
        拿打桩后的结果跟 ``state_dir()`` 比是在自问自答。
        """
        self.assertEqual(self.real_state_path().parent, settings.state_dir())
        self.assertEqual(self.real_state_path(), settings.state_dir() / journal.FILE_NAME)
        self.assertNotEqual(self.real_state_path().parent, self.path.parent)


class TestMalformedInput(JournalCase):
    """设置文件和日志都是用户能直接编辑的，什么都得扛住。"""

    def test_broken_json_reads_as_empty(self):
        self.write_raw("{not json at all")
        self.assertEqual(journal.load(self.target), {})

    def test_non_dict_root_reads_as_empty(self):
        self.write_raw([1, 2, 3])
        self.assertEqual(journal.load(self.target), {})

    def test_missing_targets_key_reads_as_empty(self):
        self.write_raw({"something": "else"})
        self.assertEqual(journal.load(self.target), {})

    def test_target_payload_that_is_not_a_dict_is_skipped(self):
        self.write_raw({"targets": {"d:/games/endfield": ["a.bin"]}})
        self.assertEqual(journal.load(self.target), {})

    def test_blank_names_are_dropped(self):
        """JSON 的键一定是字符串，所以这里只需挡住空名字。

        注意 ``"7"`` 是**合法**文件名（真有个文件叫 ``7``），不该被丢掉 ——
        把它当数字过滤掉才是 bug。
        """
        self.write_raw({"targets": {"d:/games/endfield": {"": 1, "ok.bin": 2, "7": 3}}})
        self.assertEqual(journal.load(self.target), {"ok.bin": 2, "7": 3})

    def test_booleans_are_not_sizes(self):
        """``bool`` 是 ``int`` 的子类——不单独挡掉的话 ``true`` 会变成尺寸 1。"""
        self.write_raw({"targets": {"d:/games/endfield": {"a.bin": True}}})
        self.assertEqual(journal.load(self.target), {})

    def test_non_positive_and_non_integer_sizes_are_dropped(self):
        self.write_raw({"targets": {"d:/games/endfield": {
            "zero.bin": 0, "neg.bin": -5, "text.bin": "12", "float.bin": 1.5,
        }}})
        self.assertEqual(journal.load(self.target), {})

    def test_a_healthy_entry_survives_its_broken_neighbours(self):
        self.write_raw({"targets": {"d:/games/endfield": {
            "good.bin": 42, "bad.bin": None,
        }}})
        self.assertEqual(journal.load(self.target), {"good.bin": 42})

    def test_a_write_failure_is_swallowed(self):
        """状态文件写不进去只是丢记录，绝不该让安装失败。"""
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            journal.update(self.target, added=[("a.bin", 100)])  # 不抛异常

    def test_writing_to_a_path_that_cannot_be_created_is_swallowed(self):
        with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only")):
            journal.update(self.target, added=[("a.bin", 100)])


class TestUpdateGuards(JournalCase):
    def test_bad_names_and_sizes_are_never_recorded(self):
        journal.update(self.target, added=[
            ("good.bin", 5),
            ("", 5),
            ("zero.bin", 0),
            ("neg.bin", -1),
            ("bool.bin", True),
            ("float.bin", 1.5),
            ("text.bin", "5"),
        ])
        self.assertEqual(journal.load(self.target), {"good.bin": 5})

    def test_an_empty_update_does_not_create_the_file(self):
        """什么都没变就别落盘——省得每次跑 install 都摸一遍用户的配置目录。"""
        journal.update(self.target)
        self.assertFalse(self.path.exists())

    def test_a_no_op_update_does_not_rewrite_the_file(self):
        journal.update(self.target, added=[("a.bin", 100)])
        before = self.path.stat().st_mtime_ns
        journal.update(self.target, added=[("a.bin", 100)])
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_oldest_targets_are_evicted_first(self):
        for index in range(journal.MAX_TARGETS + 2):
            journal.update(f"D:\\Games\\T{index}", added=[("a.bin", 1)])
        self.assertEqual(len(journal._read()), journal.MAX_TARGETS)
        self.assertEqual(journal.load("D:\\Games\\T0"), {})
        self.assertEqual(journal.load(f"D:\\Games\\T{journal.MAX_TARGETS + 1}"),
                         {"a.bin": 1})

    def test_touching_a_target_moves_it_to_the_back(self):
        """位置决定淘汰顺序：重新记一次就算「最近用过」。"""
        for index in range(journal.MAX_TARGETS):
            journal.update(f"D:\\Games\\T{index}", added=[("a.bin", 1)])
        journal.update("D:\\Games\\T0", added=[("b.bin", 1)])
        journal.update("D:\\Games\\TNEW", added=[("c.bin", 1)])

        self.assertEqual(journal.load("D:\\Games\\T0"), {"a.bin": 1, "b.bin": 1})
        self.assertEqual(journal.load("D:\\Games\\T1"), {})  # 最久没动的被淘汰

    def test_the_file_is_valid_json_after_every_update(self):
        journal.update(self.target, added=[("中文 名字.bin", 100)])
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["targets"][journal._key(self.target)],
                         {"中文 名字.bin": 100})

    def test_the_state_directory_does_not_leak_into_the_install_target(self):
        """日志是 EFD 自己的状态，跟游戏目录没有任何关系。"""
        target = Path(tempfile.mkdtemp(prefix="efd_target_"))
        self.addCleanup(self._rmtree, target)
        journal.update(str(target), added=[("a.bin", 1)])
        self.assertEqual(list(target.iterdir()), [])
        self.assertTrue(self.path.exists())
        self.assertNotEqual(os.path.dirname(str(self.path)), str(target))


if __name__ == "__main__":
    unittest.main()
