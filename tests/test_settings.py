"""偏好设置的读写与优先级。

测试全程把 ``config_path`` 打到临时目录，绝不去碰用户真实的
``%LOCALAPPDATA%\\EFD\\settings.json``。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from efd.core import settings
from efd.core.throttle import DEFAULT_JOBS


class SettingsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="efd_cfg_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "sub" / "settings.json"
        patcher = mock.patch.object(settings, "config_path", lambda: self.path)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestReadWrite(SettingsCase):
    def test_missing_file_reads_defaults(self):
        self.assertEqual(settings.load(), settings.Settings())
        self.assertEqual(settings.remembered_target(), "")

    def test_corrupt_file_reads_defaults(self):
        """坏掉的设置文件不能让界面起不来。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ 这不是 json", encoding="utf-8")
        self.assertEqual(settings.load(), settings.Settings())
        self.assertEqual(settings.remembered_target(), "")

    def test_non_dict_json_reads_defaults(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(settings.load(), settings.Settings())

    def test_null_json_reads_defaults(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("null", encoding="utf-8")
        self.assertEqual(settings.load(), settings.Settings())

    def test_missing_keys_keep_other_keys_intact(self):
        """缺一个键不影响另外两个——每个字段各有自己的默认值。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{"target": "D:/game"}', encoding="utf-8")
        loaded = settings.load()
        self.assertEqual(loaded.target, "D:/game")
        self.assertEqual(loaded.jobs, str(DEFAULT_JOBS))
        self.assertEqual(loaded.rate, "0")

    def test_unknown_keys_are_dropped_on_save(self):
        """用户手改文件塞进来的杂键不该被我们一直带着走。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{"target": "D:/game", "nonsense": 1}', encoding="utf-8")
        settings.remember_net("4", "0")
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(on_disk), {"target", "jobs", "rate"})
        self.assertEqual(on_disk["target"], "D:/game", "改网络选项不该顺手抹掉目录")

    def test_roundtrip(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings.remember_target(r"D:\Games\Arknights Endfield")
        self.assertEqual(settings.remembered_target(), r"D:\Games\Arknights Endfield")
        # 真的落盘了，不是只在内存里
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["target"], r"D:\Games\Arknights Endfield")

    def test_remembers_across_reads(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings.remember_target("E:/x")
        self.assertEqual(settings.remembered_target(), "E:/x")

    def test_blank_target_is_ignored(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings.remember_target("D:/keep")
        for blank in ("", "   ", "\t"):
            settings.remember_target(blank)
        self.assertEqual(settings.remembered_target(), "D:/keep")

    def test_target_that_is_not_a_string_is_ignored(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{"target": 12345}', encoding="utf-8")
        self.assertEqual(settings.remembered_target(), "")

    def test_no_write_when_nothing_changed(self):
        """重复记同一个目录不该每次都写盘。

        界面在每次点击时都会调 ``remember_*``；无变化的写入会让杀毒软件
        反复扫这个文件，也让「文件修改时间」这个调试线索失去意义。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings.remember_target("D:/same")
        first = self.path.stat().st_mtime_ns
        settings.remember_target("D:/same")
        self.assertEqual(self.path.stat().st_mtime_ns, first)

    def test_unwritable_location_does_not_raise(self):
        """写不进去也必须静默——偏好设置不值得让程序崩。"""
        with mock.patch.object(settings, "config_path",
                               side_effect=OSError("boom")):
            settings.save(settings.Settings(target="x"))  # 不抛就算过


class TestPrecedence(SettingsCase):
    def test_remembered_wins_over_detection(self):
        with mock.patch.object(settings, "remembered_target", return_value="D:/picked"):
            with mock.patch("efd.core.detect.suggest_target", return_value="D:/detected"):
                self.assertEqual(settings.initial_target(), "D:/picked")

    def test_falls_back_to_detection(self):
        with mock.patch.object(settings, "remembered_target", return_value=""):
            with mock.patch("efd.core.detect.suggest_target", return_value="D:/detected"):
                self.assertEqual(settings.initial_target(), "D:/detected")

    def test_empty_when_nothing_known(self):
        with mock.patch.object(settings, "remembered_target", return_value=""):
            with mock.patch("efd.core.detect.suggest_target", return_value=""):
                self.assertEqual(settings.initial_target(), "")


class TestNet(SettingsCase):
    """并发与限速的持久化。跟目录一样：**读不到就用出厂值，绝不报错**。"""

    def test_defaults_when_nothing_stored(self):
        jobs, rate = settings.remembered_net()
        self.assertEqual(jobs, str(DEFAULT_JOBS))
        self.assertEqual(rate, "0")

    def test_roundtrip(self):
        settings.remember_net("4", "8M")
        self.assertEqual(settings.remembered_net(), ("4", "8M"))
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["jobs"], "4")
        self.assertEqual(on_disk["rate"], "8M")

    def test_blank_values_fall_back_to_defaults(self):
        # 界面把输入框清空时不该把「空」记成一个有效设置。
        settings.remember_net("  ", "")
        jobs, rate = settings.remembered_net()
        self.assertEqual(jobs, str(DEFAULT_JOBS))
        self.assertEqual(rate, "0")

    def test_non_string_values_are_ignored(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{"jobs": 4, "rate": null}', encoding="utf-8")
        jobs, rate = settings.remembered_net()
        self.assertEqual(jobs, str(DEFAULT_JOBS))
        self.assertEqual(rate, "0")

    def test_net_and_target_do_not_clobber_each_other(self):
        """两者共用一个文件——后写的不能把先写的冲掉。"""
        settings.remember_target("D:/game")
        settings.remember_net("6", "2M")
        self.assertEqual(settings.remembered_target(), "D:/game")
        self.assertEqual(settings.remembered_net(), ("6", "2M"))

    def test_unwritable_location_does_not_raise(self):
        with mock.patch.object(settings, "config_path",
                               side_effect=OSError("boom")):
            settings.remember_net("4", "8M")  # 不抛就算过


if __name__ == "__main__":
    unittest.main()
