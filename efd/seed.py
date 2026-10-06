"""Seed 接口客户端：一次请求拿到版本号与 54 个分卷的 URL/尺寸。"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

from .config import SEED_URL, USER_AGENT, DEFAULT_TIMEOUT, seed_payload


class SeedError(RuntimeError):
    """Seed 接口返回了预期之外的东西。"""


@dataclass(frozen=True)
class Pack:
    """一个分卷。``index`` 从 1 开始，与启动器的编号一致。"""

    index: int
    url: str
    size: int


@dataclass(frozen=True)
class Release:
    version: str
    packs: tuple[Pack, ...]

    @property
    def total_bytes(self) -> int:
        return sum(p.size for p in self.packs)


def fetch_release(*, timeout: int = DEFAULT_TIMEOUT) -> Release:
    """问 Seed 要最新的全量包信息。

    请求体里自称一个较老的版本，服务端就会稳定返回全量包而不是增量。
    """
    body = json.dumps(seed_payload()).encode()
    req = urllib.request.Request(
        SEED_URL,
        data=body,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except (OSError, ValueError) as exc:
        raise SeedError(f"Seed 请求失败: {exc}") from exc

    return parse_release(payload)


def parse_release(payload: dict) -> Release:
    """从 Seed 响应里抽出发布信息。与网络解耦，便于离线测试。

    服务端结构随时可能变，所以这里对**每一层**都做类型检查，
    保证任何畸形输入都变成 :class:`SeedError`，而不是漏出 ``AttributeError``
    之类的内部异常——那样调用方就没法区分「网络问题」和「接口变了」。
    """
    if not isinstance(payload, dict):
        raise SeedError(f"响应不是 JSON 对象: {type(payload).__name__}")

    responses = payload.get("proxy_rsps")
    if not isinstance(responses, list):
        raise SeedError("响应里没有 proxy_rsps 列表")

    game = None
    for item in responses:
        if isinstance(item, dict) and item.get("kind") == "get_latest_game":
            game = item.get("get_latest_game_rsp")
            break
    if not isinstance(game, dict):
        raise SeedError("响应里没有可用的 get_latest_game")

    pkg = game.get("pkg")
    raw_packs = pkg.get("packs") if isinstance(pkg, dict) else None
    if not isinstance(raw_packs, list) or not raw_packs:
        raise SeedError("分卷列表缺失或为空")

    version = game.get("version")
    if not isinstance(version, str) or not version:
        raise SeedError("响应里没有版本号")

    packs = []
    for i, raw in enumerate(raw_packs, start=1):
        try:
            packs.append(Pack(index=i, url=raw["url"], size=int(raw["package_size"])))
        except (TypeError, KeyError, ValueError) as exc:
            raise SeedError(f"第 {i} 卷字段异常: {exc}") from exc

    return Release(version=version, packs=tuple(packs))


__all__ = ["SeedError", "Pack", "Release", "fetch_release", "parse_release"]
