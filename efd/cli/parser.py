"""命令行参数：定义、解析、以及把原始字符串解析成可用值。

参数写错必须**当场**说清楚——不能等到装了半小时才在某个线程里炸掉。
所以 ``_resolve_target`` / ``_resolve_netopts`` 都主动抛 ``SystemExit``。
"""

from __future__ import annotations

import argparse

from .. import __version__
from ..core import config, detect
from ..core.throttle import DEFAULT_JOBS, RateError, check_jobs, parse_rate


def _add_scope_args(ap: argparse.ArgumentParser) -> None:
    """plan 与 install 共用的「装什么、装到哪、怎么下」参数组。"""
    ap.add_argument("--target", metavar="DIR",
                    help="游戏安装根目录（省略时自动探测，探测失败会报错）")
    ap.add_argument("--exclude", default="", metavar="P1,P2",
                    help="跳过的路径前缀，逗号分隔（默认不跳过任何东西）")
    ap.add_argument("--exclude-ace", action="store_true",
                    help=f"跳过反作弊与 SDK：{', '.join(config.EXCLUDE_ACE)}")
    ap.add_argument("--exclude-streaming", action="store_true",
                    help="跳过 StreamingAssets（占 96%% 体积，用于试探）")
    ap.add_argument("--verify-crc", action="store_true",
                    help="对已存在的同尺寸文件算 CRC32 再决定跳过（读满全盘，很慢）")
    ap.add_argument("--force", action="store_true", help="不跳过任何已存在文件")
    ap.add_argument("--limit", type=int, default=0, metavar="N", help="只处理前 N 个文件")
    ap.add_argument("--block", type=int, default=1 << 20, metavar="N",
                    help="归档流的缓冲大小（默认 1 MiB）")
    ap.add_argument("--timeout", type=int, default=config.DEFAULT_TIMEOUT,
                    help="单次 HTTP 超时秒数")
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS, metavar="N",
                    help=f"顺序预读的并发数，1 表示不预读（默认 {DEFAULT_JOBS}；"
                         f"实测 8 比 1 快约 1.4 倍，白读不到 1%%）")
    ap.add_argument("--limit-rate", default="0", metavar="RATE",
                    help="限速，如 8M / 512K；0 表示不限（默认 0）")


def _resolve_excludes(args) -> tuple[str, ...]:
    prefixes: list[str] = [p for p in args.exclude.split(",") if p]
    if args.exclude_ace:
        prefixes.extend(config.EXCLUDE_ACE)
    if args.exclude_streaming:
        prefixes.extend(config.EXCLUDE_STREAMING)
    return tuple(prefixes)


def _resolve_target(args) -> str:
    """确定安装目录：显式给的优先，否则自动探测。

    在**打开归档之前**调用——目标定不下来就没必要先花十秒去拉清单。

    刻意不做「探测不到就用当前目录」这种兜底：这是个会往盘里写 58 GB 的
    工具，宁可报错让人显式指定，也不能猜。
    """
    if args.target:
        return args.target
    found = detect.suggest_target()
    if found:
        print(f"未指定 --target，自动探测到：{found}")
        return found
    raise SystemExit(
        "找不到游戏目录。请显式指定，例如：\n"
        '    --target "D:\\Apps\\Hypergryph Launcher\\games\\Arknights Endfield"'
    )


def _resolve_netopts(args) -> tuple[int, float]:
    """把 ``--jobs`` / ``--limit-rate`` 解析成 (jobs, bytes/s)，出错就报错退出。

    参数写错必须当场说清楚，不能等到装了半小时才在某个线程里炸掉。
    """
    try:
        jobs = check_jobs(args.jobs)
        rate = parse_rate(args.limit_rate)
    except RateError as exc:
        raise SystemExit(f"参数错误：{exc}") from None
    return jobs, rate


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="efd",
        description="《明日方舟：终末地》省空间下载器 —— 流式解包，压缩包从不落盘",
    )
    ap.add_argument("--version", action="version", version=f"efd {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="只做规划，不写任何文件")
    _add_scope_args(p_plan)
    p_plan.add_argument("--json", metavar="FILE", help="把完整计划写成 JSON")
    p_plan.add_argument("--stale", action="store_true",
                        help="额外扫描目标目录，列出清单里没有的多余文件"
                             "（慢；热更新子树除外。删除只认安装日志记过的那些）")
    p_plan.add_argument("--quiet", action="store_true", help="只打印汇总")

    p_inst = sub.add_parser("install", help="执行安装（默认仍是 dry-run）")
    _add_scope_args(p_inst)
    p_inst.add_argument("--apply", action="store_true", help="真的写盘；不加则只预览")
    p_inst.add_argument("--prune", action="store_true",
                        help="装完后删除本地多余文件。只删「安装日志记着是本工具写的"
                             "且尺寸没变过」的那些，所以游戏运行期写的存档/缓存 "
                             "永远不会被删；跳过 StreamingAssets/VFS，那是热更新的地盘")
    p_inst.add_argument("--keep-going", action="store_true",
                        help="单个文件失败时继续，而不是中断")

    p_probe = sub.add_parser("probe", help="诊断：验证 Range 能力与单文件读取成本")
    p_probe.add_argument("--count", type=int, default=3, help="抽测文件数")
    p_probe.add_argument("--timeout", type=int, default=config.DEFAULT_TIMEOUT)

    sub.add_parser("gui", help="启动图形界面")

    return ap


__all__ = [
    "build_parser",
    "_add_scope_args",
    "_resolve_excludes",
    "_resolve_netopts",
    "_resolve_target",
]
