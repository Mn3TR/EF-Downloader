"""Seed 响应解析测试（用固定报文，无网络）。"""

from __future__ import annotations

import unittest

from efd.config import seed_payload
from efd.seed import SeedError, parse_release

GOOD = {
    "proxy_rsps": [
        {"kind": "get_latest_launcher", "get_latest_launcher_rsp": {"version": "1.6.0"}},
        {
            "kind": "get_latest_game",
            "get_latest_game_rsp": {
                "version": "1.5.3",
                "pkg": {
                    "packs": [
                        {"url": "https://cdn/a.zip.001", "package_size": "1073741824"},
                        {"url": "https://cdn/a.zip.002", "package_size": "6706365"},
                    ]
                },
            },
        },
    ]
}


class TestParseRelease(unittest.TestCase):
    def test_parses_version_and_packs(self):
        rel = parse_release(GOOD)
        self.assertEqual(rel.version, "1.5.3")
        self.assertEqual(len(rel.packs), 2)
        self.assertEqual(rel.total_bytes, 1073741824 + 6706365)

    def test_pack_index_is_one_based(self):
        rel = parse_release(GOOD)
        self.assertEqual([p.index for p in rel.packs], [1, 2])

    def test_package_size_string_is_coerced(self):
        rel = parse_release(GOOD)
        self.assertIsInstance(rel.packs[0].size, int)

    def test_url_preserved(self):
        rel = parse_release(GOOD)
        self.assertEqual(rel.packs[1].url, "https://cdn/a.zip.002")


class TestParseReleaseErrors(unittest.TestCase):
    def test_no_game_response(self):
        with self.assertRaises(SeedError):
            parse_release({"proxy_rsps": []})

    def test_missing_packs(self):
        payload = {"proxy_rsps": [{"kind": "get_latest_game",
                                   "get_latest_game_rsp": {"version": "1", "pkg": {}}}]}
        with self.assertRaises(SeedError):
            parse_release(payload)

    def test_empty_packs(self):
        payload = {"proxy_rsps": [{"kind": "get_latest_game",
                                   "get_latest_game_rsp": {"version": "1",
                                                           "pkg": {"packs": []}}}]}
        with self.assertRaises(SeedError):
            parse_release(payload)

    def test_garbage(self):
        with self.assertRaises(SeedError):
            parse_release({"proxy_rsps": "not a list"})


class TestSeedPayload(unittest.TestCase):
    def test_shape(self):
        payload = seed_payload()
        kinds = [r["kind"] for r in payload["proxy_reqs"]]
        self.assertEqual(kinds, ["get_latest_launcher", "get_latest_game"])

    def test_game_request_carries_launcher_appcode(self):
        req = seed_payload()["proxy_reqs"][1]["get_latest_game_req"]
        self.assertIn("launcher_appcode", req)
        self.assertTrue(req["appcode"])
        self.assertTrue(req["version"])

    def test_seq_present(self):
        self.assertIn("seq", seed_payload())


if __name__ == "__main__":
    unittest.main()
