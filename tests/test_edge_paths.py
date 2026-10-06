"""剩余的错误分支：那些"不该发生，但一旦发生就是静默损坏"的地方。

覆盖率报告最后剩下的行几乎全是错误处理，而本项目三次事故（按钮点不动、
停止没反应、中断报完成）**全部**住在错误路径里——正常路径早就被人点过无数
遍了。所以这批测试盯的是：

* 服务器不认 Range 时**宁可报错也不写坏文件**（``efd/net/http.py``）；
* 归档条目名不可信时**既不装也不改写**，单独列出来给人看（``efd/core/planner.py``）；
* Seed 接口结构一变就报 ``SeedError``，而不是漏出 ``AttributeError``
  ——否则用户没法区分"网断了"和"官方改接口了"（``efd/core/seed.py``）。
"""

from __future__ import annotations

import http.client
import os
import tempfile
import unittest
from unittest import mock

from efd.core import planner, seed as seed_mod
from efd.core.archive import Archive, ArchiveError, load_pack_sizes, open_local
from efd.core.planner import make_plan
from efd.core.seed import SeedError, parse_release, fetch_release
from efd.net.http import HttpVolume, RangeError
from efd.net.prefetch import Prefetcher
from efd.net.volume import FileVolume, MissingVolume


# --------------------------------------------------------------- RangeError


class _FakeResp:
    def __init__(self, body: bytes, status):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestRangeGuards(unittest.TestCase):
    """服务器不按 Range 说话时必须停下——静默接受会写出损坏的文件。"""

    def volume(self, *, size=32, body=None, status=206, block=16, **kw):
        resp = _FakeResp(b"x" * (size if body is None else body), status)
        p = mock.patch("efd.net.http.urllib.request.urlopen",
                       mock.MagicMock(return_value=resp))
        p.start()
        self.addCleanup(p.stop)
        backoff = mock.patch("efd.net.http.FETCH_BACKOFF", 0.0)
        backoff.start()
        self.addCleanup(backoff.stop)
        vol = HttpVolume(7, "http://fake/vol", size, block=block, **kw)
        self.addCleanup(vol.close)
        return vol

    def test_a_200_response_is_refused(self):
        """回 200 全文件意味着偏移量全错——照收就会写坏文件。"""
        vol = self.volume(status=200)
        with self.assertRaises(RangeError) as caught:
            vol.read_at(16, 16)
        self.assertIn("200", str(caught.exception))
        self.assertIn("206", str(caught.exception))

    def test_a_200_whole_file_request_is_accepted(self):
        """但"一次要完整个卷"时的 200 是合法的，不能误杀。"""
        # block 必须不小于 size，否则取数是按块切分的，永远凑不出整卷请求。
        vol = self.volume(size=32, status=200, block=32)
        self.assertEqual(len(vol.read_at(0, 32)), 32)

    def test_a_long_response_is_refused(self):
        """要 16 字节给了 32 字节：偏移量算错了，继续写就是错位。"""
        resp = _FakeResp(b"y" * 32, 206)
        with mock.patch("efd.net.http.urllib.request.urlopen",
                        mock.MagicMock(return_value=resp)):
            vol = HttpVolume(1, "http://fake/vol", 64, block=16)
            self.addCleanup(vol.close)
            with self.assertRaises(RangeError) as caught:
                vol.read_at(0, 4)
        # 报的是实际发出去的请求长度（一个块 = 16），不是调用方要的 4。
        self.assertIn("期望 16 字节却收到 32 字节", str(caught.exception))

    def test_the_status_is_none_tolerant(self):
        """有些响应对象没有 status（自定义传输层），不能因此报错。"""
        resp = _FakeResp(b"z" * 16, None)
        with mock.patch("efd.net.http.urllib.request.urlopen",
                        mock.MagicMock(return_value=resp)):
            vol = HttpVolume(1, "http://fake/vol", 16, block=16)
            self.addCleanup(vol.close)
            self.assertEqual(vol.read_at(0, 16), b"z" * 16)


class TestHttpVolumeEdges(unittest.TestCase):
    def test_a_zero_length_request_returns_nothing(self):
        vol = HttpVolume(1, "http://fake/vol", 32)
        self.addCleanup(vol.close)
        self.assertEqual(vol.read_at(0, 0), b"")
        self.assertEqual(vol.requests, 0, "空请求不该发出去")

    def test_a_read_past_the_end_returns_nothing(self):
        vol = HttpVolume(1, "http://fake/vol", 32)
        self.addCleanup(vol.close)
        self.assertEqual(vol.read_at(32, 8), b"")
        self.assertEqual(vol.read_at(999, 8), b"")

    def test_a_negative_offset_is_clamped(self):
        """调用方算错偏移时退化成"从头读"，而不是发出 bytes=-5--1 这种头。"""
        resp = _FakeResp(b"a" * 16, 206)
        with mock.patch("efd.net.http.urllib.request.urlopen",
                        mock.MagicMock(return_value=resp)) as opener:
            vol = HttpVolume(1, "http://fake/vol", 16, block=16)
            self.addCleanup(vol.close)
            self.assertEqual(len(vol.read_at(-4, 16)), 16)
        self.assertIn("bytes=0-15", opener.call_args.args[0].get_header("Range"))

    def test_a_read_past_the_end_is_shortened(self):
        # block=size 会让偏移量 16 落在块 0 里，请求就变成整块；要看到
        # 被截短的 Range 头，块必须比偏移量小。
        resp = _FakeResp(b"b" * 8, 206)
        with mock.patch("efd.net.http.urllib.request.urlopen",
                        mock.MagicMock(return_value=resp)) as opener:
            vol = HttpVolume(1, "http://fake/vol", 24, block=8)
            self.addCleanup(vol.close)
            vol.read_at(16, 64)
        self.assertIn("bytes=16-23", opener.call_args.args[0].get_header("Range"))

    def test_cache_is_bypassed_when_disabled(self):
        """``cache_blocks=0`` 直通取数——中央目录那种一次性读取用得上。"""
        resp = _FakeResp(b"c" * 4, 206)
        with mock.patch("efd.net.http.urllib.request.urlopen",
                        mock.MagicMock(return_value=resp)) as opener:
            vol = HttpVolume(1, "http://fake/vol", 32, block=16, cache_blocks=0)
            self.addCleanup(vol.close)
            self.assertEqual(vol.cache_blocks, 0)
            vol.read_at(0, 4)
            vol.read_at(0, 4)
        self.assertEqual(opener.call_count, 2, "缓存关掉后应当每次都取数")

    def test_repr_is_informative(self):
        vol = HttpVolume(3, "http://fake/vol", 1024)
        self.addCleanup(vol.close)
        self.assertIn("index=3", repr(vol))
        self.assertIn("size=1024", repr(vol))


class TestPrefetcherEdges(unittest.TestCase):
    def test_submit_after_shutdown_returns_none(self):
        """池关了以后提交要安全返回，不能抛到安装循环里。"""
        pf = Prefetcher(4)
        pf.shutdown()
        self.assertIsNone(pf.submit(lambda: None))
        self.assertFalse(pf.enabled)

    def test_shutdown_is_idempotent(self):
        pf = Prefetcher(2)
        pf.shutdown()
        pf.shutdown()
        self.assertFalse(pf.enabled)

    def test_jobs_one_reports_disabled(self):
        self.assertFalse(Prefetcher(1).enabled)

    def test_repr_shows_the_counters(self):
        pf = Prefetcher(1)
        pf.note_used(10)
        pf.note_wasted(5)
        self.assertIn("fetched=10", repr(pf))
        self.assertIn("wasted=5", repr(pf))


# --------------------------------------------------------------- 本地分卷


class TestLocalVolumes(unittest.TestCase):
    def test_file_volume_reads_a_slice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "vol001.bin")
            with open(path, "wb") as fh:
                fh.write(bytes(range(256)))
            vol = FileVolume(path)
            try:
                self.assertEqual(vol.size, 256)
                self.assertEqual(vol.read_at(10, 4), bytes(range(10, 14)))
                self.assertIn("vol001.bin", repr(vol))
            finally:
                # Windows 上文件不关掉就删不了临时目录，所以必须在这里关，
                # 不能挂在 addCleanup 上（那要等临时目录先被删）。
                vol.close()

    def test_missing_volume_keeps_the_real_size(self):
        """尺寸对，后面的偏移量才成立——这是离线夹具能工作的原理。"""
        vol = MissingVolume(1234, 7)
        self.assertEqual(vol.size, 1234)
        vol.close()  # 不该抛
        with self.assertRaises(RuntimeError) as caught:
            vol.read_at(0, 1)
        self.assertIn("007", str(caught.exception))
        self.assertIn("1234", repr(vol))


# --------------------------------------------------------------- 归档错误


class TestArchiveErrors(unittest.TestCase):
    def test_open_rejects_an_entry_that_is_not_in_the_zip(self):
        class FakeInfo:
            filename = "only.txt"
            file_size = 1
            compress_size = 1
            CRC = 0
            header_offset = 0
            compress_type = 0

            def is_dir(self):
                return False

        zf = mock.MagicMock()
        zf.infolist.return_value = [FakeInfo()]
        ar = Archive("v", [], mock.MagicMock(), zf)
        real = ar.entries[0]
        ghost = mock.Mock(name="ghost.txt")
        with self.assertRaises(ArchiveError) as caught:
            ar.open(ghost)
        self.assertIn("ghost.txt", str(caught.exception))
        # 真条目仍然打得开
        ar.open(real)

    def test_open_local_refuses_an_empty_size_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ArchiveError) as caught:
                open_local(tmp, [])
        self.assertIn("sizes 为空", str(caught.exception))

    def test_load_pack_sizes_accepts_both_shapes(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            wrapped = os.path.join(tmp, "wrapped.json")
            bare = os.path.join(tmp, "bare.json")
            with open(wrapped, "w", encoding="utf-8") as fh:
                json.dump({"sizes": [1, 2, 3]}, fh)
            with open(bare, "w", encoding="utf-8") as fh:
                json.dump([4, 5], fh)
            self.assertEqual(load_pack_sizes(wrapped), [1, 2, 3])
            self.assertEqual(load_pack_sizes(bare), [4, 5])


# --------------------------------------------------------------- 计划边界


class TestPlannerEdges(unittest.TestCase):
    def _archive(self, entries):
        ar = mock.MagicMock()
        ar.version = "1.5.3"
        ar.total_bytes = sum(e.size for e in entries)
        ar.net_bytes = 0
        ar.entries = entries
        ar.files.return_value = [e for e in entries if not e.is_dir]
        return ar

    def _entry(self, name, size=10):
        from efd.core.archive import Entry

        return Entry(name=name, size=size, compressed=size, crc=0,
                     offset=0, method=0, is_dir=False)

    def test_an_escaping_name_is_listed_as_unsafe_not_installed(self):
        """条目名不可信时既不装也不改写——静默改写会让人找不到文件。"""
        ar = self._archive([self._entry("../../evil.txt"),
                            self._entry("ok.txt")])
        with tempfile.TemporaryDirectory() as tmp:
            plan = make_plan(ar, tmp)
        self.assertEqual(plan.unsafe, ["../../evil.txt"])
        self.assertEqual([e.name for e in plan.need], ["ok.txt"])

    def test_an_absolute_name_is_unsafe(self):
        ar = self._archive([self._entry("C:/Windows/System32/evil.dll")])
        with tempfile.TemporaryDirectory() as tmp:
            plan = make_plan(ar, tmp)
        self.assertEqual(len(plan.unsafe), 1)
        self.assertEqual(plan.need, [])

    def test_an_unreadable_destination_counts_as_missing(self):
        """读尺寸失败（权限/被占用）时当作没装，宁可从头上，不赌。"""
        ar = self._archive([self._entry("a.txt")])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(planner.os.path, "getsize",
                                   side_effect=OSError("拒绝访问")):
                plan = make_plan(ar, tmp)
        self.assertEqual([e.name for e in plan.need], ["a.txt"])

    def test_a_crc_mismatch_counts_as_missing(self):
        """``--verify-crc`` 下同尺寸但内容不同仍要重装。"""
        ar = self._archive([self._entry("a.txt", size=3)])
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.txt"), "wb") as fh:
                fh.write(b"abc")
            with mock.patch.object(planner, "crc32_file", return_value=999):
                plan = make_plan(ar, tmp, verify_crc=True)
        self.assertEqual([e.name for e in plan.need], ["a.txt"])

    def test_a_crc_read_failure_counts_as_missing(self):
        ar = self._archive([self._entry("a.txt", size=3)])
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.txt"), "wb") as fh:
                fh.write(b"abc")
            with mock.patch.object(planner, "crc32_file",
                                   side_effect=OSError("读不了")):
                plan = make_plan(ar, tmp, verify_crc=True)
        self.assertEqual([e.name for e in plan.need], ["a.txt"])

    def test_a_crc_match_is_skipped(self):
        ar = self._archive([self._entry("a.txt", size=3)])
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.txt"), "wb") as fh:
                fh.write(b"abc")
            with mock.patch.object(planner, "crc32_file", return_value=0):
                plan = make_plan(ar, tmp, verify_crc=True)
        self.assertEqual(plan.need, [])
        self.assertEqual(len(plan.already), 1)


# --------------------------------------------------------------- Seed 畸形


class TestSeedMalformed(unittest.TestCase):
    """结构一变就报 SeedError——否则用户分不清"网断了"和"官方改接口了"。"""

    def test_a_non_dict_payload_is_refused(self):
        for bad in (None, [], "nope", 42):
            with self.subTest(bad=bad):
                with self.assertRaises(SeedError):
                    parse_release(bad)

    def test_a_missing_proxy_list_is_refused(self):
        with self.assertRaises(SeedError) as caught:
            parse_release({"something": "else"})
        self.assertIn("proxy_rsps", str(caught.exception))

    def test_a_missing_game_response_is_refused(self):
        with self.assertRaises(SeedError) as caught:
            parse_release({"proxy_rsps": [{"kind": "other"}]})
        self.assertIn("get_latest_game", str(caught.exception))

    def test_a_missing_pack_list_is_refused(self):
        with self.assertRaises(SeedError) as caught:
            parse_release({"proxy_rsps": [
                {"kind": "get_latest_game",
                 "get_latest_game_rsp": {"version": "1.5.3", "pkg": {}}}]})
        self.assertIn("分卷列表", str(caught.exception))

    def test_an_empty_pack_list_is_refused(self):
        with self.assertRaises(SeedError):
            parse_release({"proxy_rsps": [
                {"kind": "get_latest_game",
                 "get_latest_game_rsp": {"version": "1.5.3",
                                         "pkg": {"packs": []}}}]})

    def test_a_missing_version_is_refused(self):
        with self.assertRaises(SeedError) as caught:
            parse_release({"proxy_rsps": [
                {"kind": "get_latest_game",
                 "get_latest_game_rsp": {
                     "pkg": {"packs": [{"url": "u", "package_size": "1"}]}}}]})
        self.assertIn("版本号", str(caught.exception))

    def test_a_pack_with_a_bad_field_is_refused_with_its_index(self):
        """报错要指出是第几卷，不然 54 卷里根本没法查。"""
        with self.assertRaises(SeedError) as caught:
            parse_release({"proxy_rsps": [
                {"kind": "get_latest_game",
                 "get_latest_game_rsp": {
                     "version": "1.5.3",
                     "pkg": {"packs": [{"url": "u", "package_size": "1"},
                                       {"url": "u"}]}}}]})
        self.assertIn("第 2 卷", str(caught.exception))

    def test_a_non_numeric_size_is_refused(self):
        with self.assertRaises(SeedError):
            parse_release({"proxy_rsps": [
                {"kind": "get_latest_game",
                 "get_latest_game_rsp": {
                     "version": "1.5.3",
                     "pkg": {"packs": [{"url": "u", "package_size": "big"}]}}}]})

    def test_a_non_dict_pkg_does_not_leak_attribute_error(self):
        with self.assertRaises(SeedError):
            parse_release({"proxy_rsps": [
                {"kind": "get_latest_game",
                 "get_latest_game_rsp": {"version": "1.5.3", "pkg": "oops"}}]})


class TestFetchRelease(unittest.TestCase):
    def test_a_network_failure_becomes_a_seed_error(self):
        with mock.patch.object(seed_mod.urllib.request, "urlopen",
                               side_effect=OSError("连不上")):
            with self.assertRaises(SeedError) as caught:
                fetch_release()
        self.assertIn("Seed 请求失败", str(caught.exception))

    def test_a_non_json_body_becomes_a_seed_error(self):
        resp = _FakeResp(b"<html>502 Bad Gateway</html>", 200)
        with mock.patch.object(seed_mod.urllib.request, "urlopen",
                               mock.MagicMock(return_value=resp)):
            with self.assertRaises(SeedError):
                fetch_release()

    def test_a_good_response_is_parsed(self):
        import json

        body = json.dumps({"proxy_rsps": [
            {"kind": "get_latest_game",
             "get_latest_game_rsp": {
                 "version": "9.9.9",
                 "pkg": {"packs": [{"url": "u1", "package_size": "10"},
                                   {"url": "u2", "package_size": "20"}]}}}]}).encode()
        resp = _FakeResp(body, 200)
        with mock.patch.object(seed_mod.urllib.request, "urlopen",
                               mock.MagicMock(return_value=resp)):
            rel = fetch_release()
        self.assertEqual(rel.version, "9.9.9")
        self.assertEqual(rel.total_bytes, 30)

    def test_the_request_asks_for_the_old_version(self):
        """请求体里自称老版本，服务端才会给全量包而不是增量。"""
        import json

        body = json.dumps({"proxy_rsps": []}).encode()
        resp = _FakeResp(body, 200)
        with mock.patch.object(seed_mod.urllib.request, "urlopen",
                               mock.MagicMock(return_value=resp)) as opener:
            with self.assertRaises(SeedError):
                fetch_release()
        req = opener.call_args.args[0]
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertTrue(req.data)


if __name__ == "__main__":
    unittest.main()
