"""把归档与计划渲染成人读的文本。

这一层是**只读**的：只接收已经算好的对象，从不发请求也不写盘。所以它可以被
单独测试——构造一个假 archive/plan 就能验证数字有没有印错。
"""

from __future__ import annotations

from collections import defaultdict

from ..core.archive import Archive
from ..core.util import human


def print_header(archive: Archive) -> None:
    print(f"游戏版本            = {archive.version}")
    print(f"归档逻辑长度        = {human(archive.total_bytes)}  ({len(archive.volumes)} 分卷)")
    print(f"清单下载成本        = {human(archive.net_bytes)}   <- 一次 Range 请求")
    files, dirs = archive.files(), archive.dirs()
    print(f"清单条目            = {len(archive.entries)}")
    print(f"文件条目            = {len(files)}   (目录条目 {len(dirs)})")
    print(f"解压后总量          = {human(sum(e.size for e in files))}")
    print(f"压缩后总量          = {human(sum(e.compressed for e in files))}")


def print_distribution(archive: Archive) -> None:
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
        # 末尾那一层必须存在：路径正好叫 ".../VFS" 时 index+1 会越界。
        # 真实数据里没有这种条目，但这是每次 plan/install 都会走到的渲染路径，
        # 一个越界就会在规划之前把整个命令打断，不值得赌。
        if "VFS" in parts and parts.index("VFS") + 1 < len(parts):
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


def print_plan(plan) -> None:
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
    if plan.foreign:
        print(f"本地外来(保留)      = {len(plan.foreign)} 个"
              f"   <- 安装日志里没有，一律不删")

    print("\n峰值磁盘估算：")
    print(f"  本方案  = 最终 {human(plan.final_bytes)} + 单文件缓冲 {human(plan.biggest)} "
          f"= {human(plan.peak_ours)}")
    print(f"  官方    = 压缩包 {human(plan.archive_bytes)} + 解压产物 "
          f"{human(plan.final_bytes)} = {human(plan.peak_official)}")
    print(f"  节省    = {human(plan.saving)}")


__all__ = ["print_distribution", "print_header", "print_plan"]
