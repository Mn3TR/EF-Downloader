"""util 的单元测试（纯本地，无网络）。"""

from __future__ import annotations

import os
import tempfile
import unittest
import zlib

from efd.core.util import (
    UnsafePathError,
    crc32_file,
    free_bytes,
    human,
    human_time,
    safe_join,
)


class TestHuman(unittest.TestCase):
    def test_units(self):
        self.assertEqual(human(0), "0.00 B")
        self.assertEqual(human(512), "512.00 B")
        self.assertEqual(human(1024), "1.00 KB")
        self.assertEqual(human(1536), "1.50 KB")
        self.assertEqual(human(1024**2), "1.00 MB")
        self.assertEqual(human(1024**3), "1.00 GB")

    def test_exact_boundary_stays_in_smaller_unit(self):
        # 1023.99 B 不应进位到 KB
        self.assertTrue(human(1023).endswith(" B"))

    def test_large_goes_to_pb(self):
        self.assertTrue(human(1024**5).endswith(" PB"))


class TestHumanTime(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(human_time(59), "00:59")
        self.assertEqual(human_time(60), "01:00")
        self.assertEqual(human_time(3661), "1:01:01")

    def test_unknown_values(self):
        for value in (0, -5, None, float("nan"), float("inf")):
            self.assertEqual(human_time(value), "--:--", msg=repr(value))


class TestSafeJoin(unittest.TestCase):
    def setUp(self):
        self.root = os.path.abspath(os.path.join(tempfile.gettempdir(), "efd_safejoin"))

    def test_normal(self):
        got = safe_join(self.root, "Endfield_Data/StreamingAssets/a.chk")
        self.assertEqual(
            got, os.path.join(self.root, "Endfield_Data", "StreamingAssets", "a.chk")
        )

    def test_backslash_is_normalised(self):
        self.assertEqual(safe_join(self.root, "a\\b"), safe_join(self.root, "a/b"))

    def test_dot_components_are_dropped(self):
        self.assertEqual(safe_join(self.root, "./a/./b"), safe_join(self.root, "a/b"))

    def test_rejects_traversal(self):
        for bad in ("../evil", "a/../../evil", "..", "a/.."):
            with self.assertRaises(UnsafePathError, msg=bad):
                safe_join(self.root, bad)

    def test_rejects_absolute(self):
        for bad in ("/etc/passwd", "C:/Windows/system32", "c:/x"):
            with self.assertRaises(UnsafePathError, msg=bad):
                safe_join(self.root, bad)

    def test_stays_inside_root(self):
        # 正常条目必须落在 root 之下
        got = safe_join(self.root, "a/b/c")
        self.assertTrue(os.path.abspath(got).startswith(self.root))


class TestCrc32(unittest.TestCase):
    def test_matches_zlib(self):
        payload = bytes(range(256)) * 97  # 24832 字节
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(payload)
            path = f.name
        try:
            self.assertEqual(crc32_file(path), zlib.crc32(payload) & 0xFFFFFFFF)
            # 小 chunk 也必须得到同样的结果（跨块累加正确）
            self.assertEqual(crc32_file(path, chunk=7), zlib.crc32(payload) & 0xFFFFFFFF)
        finally:
            os.remove(path)

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            self.assertEqual(crc32_file(path), 0)
        finally:
            os.remove(path)


class TestFreeBytes(unittest.TestCase):
    def test_existing_dir(self):
        self.assertGreater(free_bytes(tempfile.gettempdir()), 0)

    def test_nonexistent_path_walks_up(self):
        ghost = os.path.join(tempfile.gettempdir(), "efd_no_such_dir_xyz", "deeper")
        self.assertGreater(free_bytes(ghost), 0)


if __name__ == "__main__":
    unittest.main()
