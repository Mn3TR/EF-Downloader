"""自动探测游戏目录的测试。

纯函数部分（识别标志、扫 games/）完全脱离 Windows 与注册表，
用临时目录就能测；注册表那层用 mock 顶掉。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from efd.core import detect


class TestLooksLikeGameDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="efd_detect_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_marker_file(self):
        game = self.tmp / "g1"
        game.mkdir()
        (game / "Endfield.exe").write_bytes(b"")
        self.assertTrue(detect.looks_like_game_dir(game))

    def test_marker_dir(self):
        game = self.tmp / "g2"
        (game / "Endfield_Data").mkdir(parents=True)
        self.assertTrue(detect.looks_like_game_dir(game))

    def test_empty_dir_is_not_a_game(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertFalse(detect.looks_like_game_dir(empty))

    def test_nonexistent_path(self):
        self.assertFalse(detect.looks_like_game_dir(self.tmp / "nope"))

    def test_garbage_input_does_not_raise(self):
        for bad in (None, 0, object()):
            self.assertFalse(detect.looks_like_game_dir(bad), msg=repr(bad))


class TestScanGamesDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="efd_scan_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.games = self.tmp / "games"
        self.games.mkdir()

    def test_finds_the_game_subdir(self):
        other = self.games / "SomeOtherGame"
        other.mkdir()
        (other / "whatever.dat").write_bytes(b"")
        target = self.games / "Arknights Endfield"
        target.mkdir()
        (target / "Endfield.exe").write_bytes(b"")

        self.assertEqual(detect.scan_games_dir(self.games), str(target))

    def test_none_when_no_game_present(self):
        (self.games / "SomethingElse").mkdir()
        self.assertIsNone(detect.scan_games_dir(self.games))

    def test_none_for_missing_dir(self):
        self.assertIsNone(detect.scan_games_dir(self.tmp / "does_not_exist"))

    def test_ignores_loose_files(self):
        (self.games / "Endfield.exe").write_bytes(b"")  # 是文件，不是目录
        self.assertIsNone(detect.scan_games_dir(self.games))


class TestLauncherPaths(unittest.TestCase):
    def test_returns_list(self):
        self.assertIsInstance(detect.launcher_paths(), list)

    def test_wrong_hive_name_raises_instead_of_silently_returning_empty(self):
        """hive 常量名写错时必须炸，不能静默返回空。

        这正是一次真实事故：REGISTRY_KEYS 里写了 'HKCU' 而不是
        'HKEY_CURRENT_USER'，getattr 拿到 None 被跳过，探测永远返回空，
        而所有测试照样通过。
        """
        with mock.patch.object(detect, "REGISTRY_KEYS",
                               (("HKDOESNOTEXIST", r"Software\Whatever"),)):
            with self.assertRaises(RuntimeError):
                detect.launcher_paths()


class TestFindGameDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="efd_find_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.launcher = self.tmp / "Hypergryph Launcher"

    def test_none_when_no_launcher(self):
        with mock.patch.object(detect, "launcher_paths", return_value=[]):
            self.assertIsNone(detect.find_game_dir())

    def test_finds_installed_game_under_games(self):
        game = self.launcher / "games" / "Arknights Endfield"
        game.mkdir(parents=True)
        (game / "Endfield.exe").write_bytes(b"")

        with mock.patch.object(detect, "launcher_paths", return_value=[str(self.launcher)]):
            self.assertEqual(detect.find_game_dir(), str(game))

    def test_falls_back_to_launcher_dir_itself(self):
        self.launcher.mkdir(parents=True)
        (self.launcher / "Endfield_Data").mkdir()

        with mock.patch.object(detect, "launcher_paths", return_value=[str(self.launcher)]):
            self.assertEqual(detect.find_game_dir(), str(self.launcher))


class TestSuggestTarget(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="efd_suggest_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_empty_when_nothing_found(self):
        """探测不到就必须返回空串，让用户自己选——绝不能编一个假路径。"""
        with mock.patch.object(detect, "launcher_paths", return_value=[]):
            self.assertEqual(detect.suggest_target(), "")

    def test_prefers_installed_game(self):
        launcher = self.tmp / "Launcher"
        game = launcher / "games" / "Arknights Endfield"
        game.mkdir(parents=True)
        (game / "Endfield.exe").write_bytes(b"")

        with mock.patch.object(detect, "launcher_paths", return_value=[str(launcher)]):
            self.assertEqual(detect.suggest_target(), str(game))

    def test_falls_back_to_conventional_path(self):
        """只有启动器、还没装游戏时，填约定路径（安装会自己建目录）。"""
        launcher = self.tmp / "Launcher"
        launcher.mkdir(parents=True)

        with mock.patch.object(detect, "launcher_paths", return_value=[str(launcher)]):
            expected = str(launcher / "games" / detect.DEFAULT_GAME_NAME)
            self.assertEqual(detect.suggest_target(), expected)


class TestRealEnvironment(unittest.TestCase):
    """在真实机器上验证。

    这一组**故意依赖环境**：注册表里确实有启动器记录时，探测就必须找到它。

    之前这里只写了「调用不抛异常」——于是 hive 名字写错、探测永远返回空，
    测试依然全绿。那种测试比没有更糟，因为它制造了「已验证」的假象。
    """

    def test_hive_names_are_real_winreg_constants(self):
        if os.name != "nt":
            self.skipTest("仅 Windows")
        import winreg

        for hive_name, _subkey in detect.REGISTRY_KEYS:
            self.assertTrue(
                hasattr(winreg, hive_name),
                f"winreg 里没有 {hive_name}（REGISTRY_KEYS 写错了）",
            )

    def test_detects_launcher_when_registry_says_so(self):
        """注册表里有 install_path，launcher_paths() 就必须读出来。"""
        if os.name != "nt":
            self.skipTest("仅 Windows")
        import winreg

        expected: list[str] = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Hypergryph\Launcher") as key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    child = winreg.EnumKey(key, index)
                    try:
                        with winreg.OpenKey(key, child) as sub:
                            value, _ = winreg.QueryValueEx(sub, "install_path")
                            expected.append(value)
                    except OSError:
                        pass
        except OSError:
            self.skipTest("这台机器没装鹰角启动器")

        if not expected:
            self.skipTest("注册表里有键但没有 install_path")

        got = detect.launcher_paths()
        for path in expected:
            self.assertIn(path, got,
                          f"注册表里明明有 {path!r}，launcher_paths() 却没读到")

    def test_found_game_dir_really_is_a_game_dir(self):
        found = detect.find_game_dir()
        if found is None:
            self.skipTest("没探测到游戏目录")
        self.assertTrue(Path(found).is_dir(), found)
        self.assertTrue(detect.looks_like_game_dir(found), found)

    def test_suggest_target_is_sane(self):
        value = detect.suggest_target()
        if not value:
            self.skipTest("什么都没探测到")
        self.assertTrue(
            Path(value).exists() or "games" in value.lower(),
            f"建议的路径看着可疑：{value}",
        )


if __name__ == "__main__":
    unittest.main()
