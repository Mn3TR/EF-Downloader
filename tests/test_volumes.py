"""ConcatReader 的边界测试（纯内存，无网络）。

跨卷读取是整个方案的地基：偏移量算错一点点，解压出来的就是垃圾。
"""

from __future__ import annotations

import io
import unittest

from efd.volumes import ConcatReader, MissingVolume, open_stream


class MemoryVolume:
    """内存分卷，用于测试。"""

    def __init__(self, data: bytes, index: int = 0):
        self.data = data
        self.size = len(data)
        self.index = index

    def read_at(self, offset: int, n: int) -> bytes:
        return self.data[offset : offset + n]

    def close(self) -> None:
        pass


class ShortVolume(MemoryVolume):
    """谎报尺寸、实际给不出数据的坏卷。"""

    def read_at(self, offset: int, n: int) -> bytes:
        return b""


def reader(*chunks: bytes) -> ConcatReader:
    return ConcatReader([MemoryVolume(c, i) for i, c in enumerate(chunks)])


class TestConcatReader(unittest.TestCase):
    def test_total(self):
        self.assertEqual(reader(b"abc", b"de", b"f").total, 6)

    def test_sequential_read(self):
        r = reader(b"abc", b"de", b"f")
        self.assertEqual(r.read(), b"abcde" + b"f")

    def test_read_across_boundary(self):
        r = reader(b"abc", b"defg")
        self.assertEqual(r.read(5), b"abcde")

    def test_bounded_reads_walk_correctly(self):
        r = reader(b"ab", b"cd", b"ef")
        self.assertEqual([r.read(1) for _ in range(6)], [b"a", b"b", b"c", b"d", b"e", b"f"])
        self.assertEqual(r.read(1), b"")

    def test_read_single_byte_spans_many_volumes(self):
        r = reader(b"a", b"b", b"c", b"d", b"e")
        self.assertEqual(r.read(5), b"abcde")

    def test_seek_set_cur_end(self):
        r = reader(b"abcdef")
        self.assertEqual(r.seek(2), 2)
        self.assertEqual(r.read(1), b"c")
        self.assertEqual(r.seek(1, io.SEEK_CUR), 4)
        self.assertEqual(r.read(1), b"e")
        self.assertEqual(r.seek(-1, io.SEEK_END), 5)
        self.assertEqual(r.read(1), b"f")

    def test_seek_clamps(self):
        r = reader(b"abc")
        self.assertEqual(r.seek(-100), 0)
        self.assertEqual(r.seek(999), 3)
        self.assertEqual(r.read(), b"")

    def test_seek_after_end_then_read_is_empty(self):
        r = reader(b"abc", b"def")
        r.seek(10)
        self.assertEqual(r.read(), b"")

    def test_invalid_whence(self):
        with self.assertRaises(ValueError):
            reader(b"abc").seek(0, 99)

    def test_read_none_and_negative(self):
        r = reader(b"abc")
        self.assertEqual(r.read(None), b"abc")
        r.seek(0)
        self.assertEqual(r.read(-1), b"abc")

    def test_tell(self):
        r = reader(b"abc", b"def")
        r.read(4)
        self.assertEqual(r.tell(), 4)

    def test_readinto(self):
        r = reader(b"abc", b"def")
        buf = bytearray(4)
        self.assertEqual(r.readinto(buf), 4)
        self.assertEqual(bytes(buf), b"abcd")

    def test_readinto_at_eof(self):
        r = reader(b"abc")
        r.seek(0)
        r.read(3)
        self.assertEqual(r.readinto(bytearray(4)), 0)

    def test_truncated_volume_raises(self):
        r = ConcatReader([ShortVolume(b"x" * 10)])
        with self.assertRaises(RuntimeError):
            r.read(4)

    def test_missing_volume_raises_and_keeps_offsets(self):
        """缺失分卷仍要贡献正确的偏移量——离线夹具就靠这个。"""
        r = ConcatReader([MissingVolume(1000, 1), MemoryVolume(b"tail", 2)])
        self.assertEqual(r.total, 1004)
        self.assertEqual(r.seek(1000), 1000)
        self.assertEqual(r.read(4), b"tail")
        r.seek(0)
        with self.assertRaises(RuntimeError):
            r.read(1)

    def test_is_rawio_and_buffered_wraps(self):
        stream = open_stream([MemoryVolume(b"hello world")], buffer_size=4)
        try:
            self.assertIsInstance(stream.raw, ConcatReader)
            self.assertEqual(stream.read(), b"hello world")
        finally:
            stream.close()

    def test_close_closes_volumes(self):
        r = reader(b"abc")
        r.close()
        self.assertTrue(r.closed)


if __name__ == "__main__":
    unittest.main()
