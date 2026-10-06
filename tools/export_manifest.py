"""从离线夹具导出中央目录清单为 CSV。

    python tools/export_manifest.py

产物 ``data/package_manifest.csv`` 是**派生物**，不是手工维护的清单：
它由 ``data/fixtures/vol054.bin`` + ``pack_sizes.json`` 确定性重建。

（旧版 ``cd_fixed.csv`` 是 PowerShell ``Export-Csv`` 的产物，表头被套了两层引号
并带 UTF-8 BOM，字段名成了 ``"Name"``；那个文件已删除。）
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from efd.core.archive import load_pack_sizes, open_local  # noqa: E402

FIXTURES = ROOT / "data" / "fixtures"
OUT = ROOT / "data" / "package_manifest.csv"

METHODS = {0: "Stored", 8: "Deflated", 12: "Bzip2", 14: "Lzma", 93: "Zstd"}


def main() -> int:
    sizes = load_pack_sizes(str(FIXTURES / "pack_sizes.json"))
    with open_local(str(FIXTURES), sizes, version="fixture") as archive:
        entries = archive.entries
        # newline="" 交给 csv 模块处理换行；lineterminator 固定成 \n 保证跨平台可复现
        with open(OUT, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(["Name", "Kind", "Method", "Crc", "CSize", "USize", "Lho"])
            for entry in entries:
                writer.writerow([
                    entry.name,
                    "dir" if entry.is_dir else "file",
                    METHODS.get(entry.method, str(entry.method)),
                    entry.crc,
                    entry.compressed,
                    entry.size,
                    entry.offset,
                ])

    print(f"写出 {len(entries)} 行 -> {OUT}")
    files = sum(1 for e in entries if not e.is_dir)
    print(f"  文件 {files} / 目录 {len(entries) - files}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
