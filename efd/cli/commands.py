"""四个子命令的实现。

每个 ``cmd_*`` 都是 ``(args) -> int``（退出码），不打印解析层的错误——那些由
``runner.main`` 统一捕获并翻译。这一层只负责编排：解析参数 → 打开归档 →
算计划 → 交给 core 执行 → 打印结果。
"""

from __future__ import annotations

import json
import zipfile

from ..core import config
from ..core.archive import open_remote
from ..core.installer import install
from ..core.planner import make_plan
from ..core.throttle import format_rate
from ..core.util import human, human_time
from .exitcode import EXIT_ERROR, EXIT_OK, EXIT_PARTIAL
from .parser import _resolve_excludes, _resolve_netopts, _resolve_target
from .report import print_distribution, print_header, print_plan


def cmd_plan(args) -> int:
    """只做规划：拉清单、算峰值磁盘、可选写 JSON。绝不写游戏文件。"""
    target = _resolve_target(args)
    jobs, limit_rate = _resolve_netopts(args)
    with open_remote(block=args.block, jobs=jobs, limit_rate=limit_rate) as archive:
        if not args.quiet:
            print_header(archive)
            print_distribution(archive)
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
        print_plan(plan)

        if args.json:
            # 写全量，不截断——截断过的「计划」会骗人。
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(plan.to_dict(), f, ensure_ascii=False, indent=2)
            print(f"\n完整计划已写出 -> {args.json}")

    print("\n[dry-run] 未修改任何文件")
    return EXIT_OK


def cmd_install(args) -> int:
    """执行安装。默认仍是 dry-run，加 ``--apply`` 才真写盘。"""
    target = _resolve_target(args)
    jobs, limit_rate = _resolve_netopts(args)
    with open_remote(block=args.block, jobs=jobs, limit_rate=limit_rate) as archive:
        plan = make_plan(
            archive,
            target,
            exclude_prefixes=_resolve_excludes(args),
            verify_crc=args.verify_crc,
            force=args.force,
            limit=args.limit,
            detect_stale=args.prune,
        )
        print_header(archive)
        print("\n===== 计划 =====")
        print_plan(plan)

        if not plan.need and not args.prune:
            print("\n没有需要安装的文件。")
            return EXIT_OK

        if not args.apply:
            print("\n[dry-run] 未写任何文件。加 --apply 才真装。")
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
    if archive.prefetched_bytes or archive.prefetch_wasted_bytes:
        # 如实报账：预读赚了多少、白花多少。白花的比例高就说明 jobs 开大了。
        got = archive.prefetched_bytes
        lost = archive.prefetch_wasted_bytes
        share = f"{got / (got + lost):.0%}" if got + lost else "—"
        print(f"预读     = 用上 {human(got)} / 白读 {human(lost)}（有效 {share}）")
        if archive.limiter is not None and archive.limiter.waited:
            print(f"限速     = 上限 {format_rate(archive.limiter.rate)}，"
                  f"累计等待 {human_time(archive.limiter.waited)}")
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
    # 诊断命令刻意不预读、不限速：它量的是单流真实成本，加了并发就不准了。
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
    """启动图形界面。

    导入写在函数体内：``plan`` / ``install`` 不该为了跑命令行而加载 tkinter。
    """
    from ..ui.entry import main as gui_main

    gui_main()
    return EXIT_OK


__all__ = ["cmd_gui", "cmd_install", "cmd_plan", "cmd_probe"]
