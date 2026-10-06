"""命令行入口。

    python -m efd plan    --target "D:\\...\\Arknights Endfield"
    python -m efd install --target "D:\\...\\Arknights Endfield" --apply
    python -m efd probe
    python -m efd gui
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections import defaultdict

from . import __version__, config, detect
from .archive import Archive, open_remote
from .installer import install
from .planner import make_plan
from .seed import SeedError
from .util import human, human_time, setup_output_encoding
from .volumes import RangeError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PARTIAL = 2


# ---------------------------------------------------------------- 参数


def _add_scope_args(ap: argparse.ArgumentParser) -> None:
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
                        help="额外扫描目标目录，列出清单里没有的多余文件（慢）")
    p_plan.add_argument("--quiet", action="store_true", help="只打印汇总")

    p_inst = sub.add_parser("install", help="执行安装（默认仍是 dry-run）")
    _add_scope_args(p_inst)
    p_inst.add_argument("--apply", action="store_true", help="真的写盘；不加则只预览")
    p_inst.add_argument("--prune", action="store_true",
                        help="装完后删除本地多余文件（清单里已不存在的）")
    p_inst.add_argument("--keep-going", action="store_true",
                        help="单个文件失败时继续，而不是中断")

    p_probe = sub.add_parser("probe", help="诊断：验证 Range 能力与单文件读取成本")
    p_probe.add_argument("--count", type=int, default=3, help="抽测文件数")
    p_probe.add_argument("--timeout", type=int, default=config.DEFAULT_TIMEOUT)

    sub.add_parser("gui", help="启动图形界面")

    return ap


# ---------------------------------------------------------------- 输出


def _print_header(archive: Archive) -> None:
    print(f"游戏版本            = {archive.version}")
    print(f"归档逻辑长度        = {human(archive.total_bytes)}  ({len(archive.volumes)} 分卷)")
    print(f"清单下载成本        = {human(archive.net_bytes)}   <- 一次 Range 请求")
    files, dirs = archive.files(), archive.dirs()
    print(f"清单条目            = {len(archive.entries)}")
    print(f"文件条目            = {len(files)}   (目录条目 {len(dirs)})")
    print(f"解压后总量          = {human(sum(e.size for e in files))}")
    print(f"压缩后总量          = {human(sum(e.compressed for e in files))}")


def _print_distribution(archive: Archive) -> None:
    files = archive.files()

    by_top: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e in files:
        by_top[e.name.split("/")[0]][0] += 1
        by_top[e.name.split("/")[0]][1] += e.size
    print("\n顶层分布（按解压体积降序）:")
    for name, (count, size) in sorted(by_top.items(), key=lambda kv: -kv[1][1])[:10]:
        print(f"   {name:<28} {count:>5} 个  {human(size):>12}")

    by_blk: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e in files:
        parts = e.name.split("/")
        if "VFS" in parts:
            key = parts[parts.index("VFS") + 1]
            by_blk[key][0] += 1
            by_blk[key][1] += e.size
    if by_blk:
        print("\nVFS 块分布:")
        for key, (count, size) in sorted(by_blk.items(), key=lambda kv: -kv[1][1]):
            print(f"   {key:<10} {count:>5} 个  {human(size):>12}")

    print("\n最大的 8 个文件:")
    for e in sorted(files, key=lambda x: -x.size)[:8]:
        print(f"   {human(e.size):>12}  {e.name}")


def _print_plan(plan) -> None:
    print(f"目标目录            = {plan.target}")
    print(f"已存在跳过          = {plan.already_count} 个  {human(plan.already_bytes)}")
    if plan.excluded:
        print(f"排除                = {len(plan.excluded)} 个  "
              f"{human(sum(e.size for e in plan.excluded))}")
    print(f"本次安装            = {plan.need_count} 个文件")
    print(f"  解压后            = {human(plan.need_uncompressed)}")
    print(f"  需下载(压缩)      = {human(plan.need_compressed)}")
    print(f"  最大单文件        = {human(plan.biggest)}   <- 缓冲/续传粒度")
    if plan.unsafe:
        print(f"  !! 路径不可信     = {len(plan.unsafe)} 个（已跳过，见 --json）")
    if plan.stale:
        print(f"本地多余(陈旧)      = {len(plan.stale)} 个")

    print("\n峰值磁盘估算：")
    print(f"  本方案  = 最终 {human(plan.final_bytes)} + 单文件缓冲 {human(plan.biggest)} "
          f"= {human(plan.peak_ours)}")
    print(f"  官方    = 压缩包 {human(plan.archive_bytes)} + 解压产物 "
          f"{human(plan.final_bytes)} = {human(plan.peak_official)}")
    print(f"  节省    = {human(plan.saving)}")


# ---------------------------------------------------------------- 子命令


def cmd_plan(args) -> int:
    target = _resolve_target(args)
    with open_remote(block=args.block) as archive:
        if not args.quiet:
            _print_header(archive)
            _print_distribution(archive)
        plan = make_plan(
            archive,
            target,
            exclude_prefixes=_resolve_excludes(args),
            verify_crc=args.verify_crc,
            force=args.force,
            limit=args.limit,
            detect_stale=args.stale,
        )
        print("\n===== 计划 =====")
        _print_plan(plan)

        if args.json:
            # 写全量，不截断——截断过的「计划」会骗人。
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(plan.to_dict(), f, ensure_ascii=False, indent=2)
            print(f"\n完整计划已写出 -> {args.json}")

    print("\n[dry-run] 未修改任何文件")
    return EXIT_OK


def cmd_install(args) -> int:
    target = _resolve_target(args)
    with open_remote(block=args.block) as archive:
        plan = make_plan(
            archive,
            target,
            exclude_prefixes=_resolve_excludes(args),
            verify_crc=args.verify_crc,
            force=args.force,
            limit=args.limit,
            detect_stale=args.prune,
        )
        _print_header(archive)
        print("\n===== 计划 =====")
        _print_plan(plan)

        if not plan.need:
            print("\n没有需要安装的文件。")
            return EXIT_OK

        if not args.apply:
            print(f"\n[dry-run] 未写任何文件。加 --apply 才真装。")
            print(f"目标目录 = {plan.target}")
            return EXIT_OK

        print(f"\n开始安装到 {plan.target}\n")

        def on_progress(p) -> None:
            line = (f"  {p.done}/{p.total}  {human(p.written)}  "
                    f"{human(p.speed)}/s  剩余 {human_time(p.eta)}")
            print(line, flush=True)

        result = install(
            archive, plan,
            on_progress=on_progress,
            keep_going=args.keep_going,
            prune=args.prune,
            interval=10.0,
        )

    print()
    if result.stopped:
        print(f"已停止：{result.done} 个文件，写出 {human(result.written)}")
    else:
        print(f"完成：{result.done} 个文件，写出 {human(result.written)}")
    print(f"网络下载 = {human(result.net_bytes)}   请求数 = {result.requests}")
    print(f"峰值磁盘 ≈ {human(result.written + plan.biggest)}  （未落盘任何压缩包）")
    if result.pruned:
        print(f"已清理多余文件 = {result.pruned} 个")
    print(f"耗时 = {human_time(result.elapsed)}")

    if result.errors:
        print(f"\n失败 {len(result.errors)} 个：")
        for message in result.errors[:20]:
            print(f"  {message}")
        if len(result.errors) > 20:
            print(f"  … 另有 {len(result.errors) - 20} 个，见日志")
        return EXIT_PARTIAL
    return EXIT_OK


def cmd_probe(args) -> int:
    """抽测若干中段小文件，量化「精确抠出一个文件」的真实下载量。

    这就是当初验证 Range 可行性的那个实验，现在是可重复执行的诊断命令。
    """
    range_log: list = []
    with open_remote(block=1 << 16, range_log=range_log) as archive:
        infos = [e for e in archive.files()
                 if 10 <= e.offset // config.VOLUME_SIZE <= 40
                 and 20_000 <= e.compressed <= 400_000
                 and e.method == zipfile.ZIP_DEFLATED]
        infos.sort(key=lambda e: e.compressed)
        print(f"中段候选 = {len(infos)}")
        if not infos:
            print("没有符合条件的中段小文件。")
            return EXIT_ERROR

        picked = infos[: args.count]
        before = archive.net_bytes
        print(f"清单解析已花费 = {human(before)} / {archive.net_requests} 次请求\n")

        for entry in picked:
            volume = entry.offset // config.VOLUME_SIZE + 1
            data = archive.read(entry)  # 解压 + CRC32 由标准库校验
            if len(data) != entry.size:
                print(f"  FAIL {entry.name}: 长度 {len(data)} != {entry.size}")
                return EXIT_ERROR
            print(f"  OK  卷{volume:03d}  {entry.name[:58]:<58} "
                  f"c={entry.compressed:>8,} u={entry.size:>9,}  CRC32 通过")

        spent = archive.net_bytes - before
        expected = sum(e.compressed for e in picked)
        print(f"\n{len(picked)} 个文件的压缩数据总量 = {expected:,} B")
        print(f"实际下载                 = {spent:,} B（含缓冲预读）")
        print(f"若按整卷下载会是         = {len(picked) * config.VOLUME_SIZE:,} B")
        print(f"总请求数 = {archive.net_requests}   总下载 = {human(archive.net_bytes)}")

        if range_log:
            print("\nRange 请求明细（卷, 起始, 长度）:")
            for volume, offset, length in range_log[:20]:
                print(f"   卷{volume:03d}  off={offset:>15,}  len={length:>10,}")
    return EXIT_OK


def cmd_gui(args) -> int:
    from .gui import main as gui_main

    gui_main()
    return EXIT_OK


COMMANDS = {
    "plan": cmd_plan,
    "install": cmd_install,
    "probe": cmd_probe,
    "gui": cmd_gui,
}


def main(argv: list[str] | None = None) -> int:
    setup_output_encoding()
    if argv is None:
        argv = sys.argv[1:]
        if not argv and getattr(sys, "frozen", False):
            # 双击 exe 时没有任何参数，此时直接进图形界面——这是「双击」
            # 这个动作唯一合理的语义。显式传入 argv（例如测试）不受影响。
            argv = ["gui"]
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except KeyboardInterrupt:
        print("\n已中断。重新运行会自动续传（已装好的文件不会重下）。")
        return EXIT_PARTIAL
    except SeedError as exc:
        print(f"接口错误：{exc}", file=sys.stderr)
        print("Seed 接口不是公开 API，服务端结构可能已变。", file=sys.stderr)
        return EXIT_ERROR
    except RangeError as exc:
        print(f"下载错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"系统错误：{exc}", file=sys.stderr)
        return EXIT_ERROR


__all__ = ["main", "build_parser"]
