"""生成 exe 用的图标。

    python tools/make_icon.py

手写 ICO，不依赖 Pillow —— 这个项目零依赖是条底线，构建期也一样。

图形很简单：圆角方块底 + 向下的箭头 + 底部一条横杠（「下载到磁盘」）。
没有文字，所以不需要字体。用 4 倍超采样再盒式缩小来获得抗锯齿边缘。

产物 ``packaging/efd.ico`` 会入库，这样重新构建 exe 不需要先跑这个脚本。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging" / "efd.ico"

SIZES = (16, 32, 48, 64, 128, 256)
SUPERSAMPLE = 4

# 配色
TOP = (0x3B, 0x82, 0xF6)  # 天蓝
BOTTOM = (0x1D, 0x4E, 0xD8)  # 深蓝
FG = (0xFF, 0xFF, 0xFF)


# ---------------------------------------------------------------- 形状
# 全部在归一化坐标（0..1）里判定，与分辨率无关。

def _in_round_rect(x: float, y: float, x0=0.02, y0=0.02, x1=0.98, y1=0.98,
                   r: float = 0.22) -> bool:
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    # 四个角用圆形裁掉
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def _in_rect(x: float, y: float, x0, y0, x1, y1) -> bool:
    return x0 <= x <= x1 and y0 <= y <= y1


def _in_triangle(x: float, y: float, ax, ay, bx, by, cx, cy) -> bool:
    def sign(px, py, qx, qy, rx, ry):
        return (px - rx) * (qy - ry) - (qx - rx) * (py - ry)

    d1 = sign(x, y, ax, ay, bx, by)
    d2 = sign(x, y, bx, by, cx, cy)
    d3 = sign(x, y, cx, cy, ax, ay)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def _is_foreground(x: float, y: float) -> bool:
    # 箭杆
    if _in_rect(x, y, 0.435, 0.175, 0.565, 0.475):
        return True
    # 箭头
    if _in_triangle(x, y, 0.285, 0.435, 0.715, 0.435, 0.5, 0.685):
        return True
    # 底部横杠（两端切圆）
    if _in_round_rect(x, y, 0.255, 0.775, 0.745, 0.865, r=0.045):
        return True
    return False


def _background(x: float, y: float, size: int) -> tuple[int, int, int, int] | None:
    if not _in_round_rect(x, y):
        return None
    t = y  # 纵向渐变
    r = round(TOP[0] + (BOTTOM[0] - TOP[0]) * t)
    g = round(TOP[1] + (BOTTOM[1] - TOP[1]) * t)
    b = round(TOP[2] + (BOTTOM[2] - TOP[2]) * t)
    return (r, g, b, 255)


def render(size: int) -> list[list[tuple[int, int, int, int]]]:
    """返回 size×size 的 RGBA 像素（行优先，自上而下）。"""
    n = size * SUPERSAMPLE
    # 先在超采样分辨率上算 RGBA
    hi: list[list[tuple[int, int, int, int]]] = []
    for j in range(n):
        y = (j + 0.5) / n
        row = []
        for i in range(n):
            x = (i + 0.5) / n
            bg = _background(x, y, size)
            if bg is None:
                row.append((0, 0, 0, 0))
            elif _is_foreground(x, y):
                row.append((*FG, 255))
            else:
                row.append(bg)
        hi.append(row)

    # 盒式缩小
    out: list[list[tuple[int, int, int, int]]] = []
    area = SUPERSAMPLE * SUPERSAMPLE
    for j in range(size):
        row = []
        for i in range(size):
            ar = ag = ab = aa = 0
            for dj in range(SUPERSAMPLE):
                line = hi[j * SUPERSAMPLE + dj]
                for di in range(SUPERSAMPLE):
                    r, g, b, a = line[i * SUPERSAMPLE + di]
                    # 预乘 alpha，避免边缘出现黑边
                    ar += r * a
                    ag += g * a
                    ab += b * a
                    aa += a
            if aa == 0:
                row.append((0, 0, 0, 0))
            else:
                row.append((ar // aa, ag // aa, ab // aa, aa // area))
        out.append(row)
    return out


# ---------------------------------------------------------------- ICO


def _dib(pixels: list[list[tuple[int, int, int, int]]]) -> bytes:
    """BITMAPINFOHEADER + BGRA 位图 + AND 掩码（ICO 里的标准 DIB 形态）。"""
    size = len(pixels)
    header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        size,        # biWidth
        size * 2,    # biHeight —— 高度写两倍，因为后面还跟着 AND 掩码
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression = BI_RGB
        0,           # biSizeImage
        0, 0,        # 分辨率
        0, 0,        # 调色板
    )

    xor = bytearray()
    for row in reversed(pixels):  # DIB 自下而上
        for r, g, b, a in row:
            xor += bytes((b, g, r, a))

    # AND 掩码：32bpp 下不参与透明判定，但格式要求必须存在。
    stride = ((size + 31) // 32) * 4
    and_mask = bytes(stride * size)

    return header + bytes(xor) + and_mask


def write_png(path: Path, pixels) -> None:
    """把 RGBA 像素写成 PNG。只用于人工预览，不进 exe。"""
    import zlib

    height = len(pixels)
    width = len(pixels[0])
    raw = b"".join(b"\x00" + b"".join(bytes(px) for px in row) for row in pixels)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def build_ico(path: Path, sizes=SIZES) -> None:
    images = [_dib(render(s)) for s in sizes]

    out = bytearray()
    out += struct.pack("<HHH", 0, 1, len(sizes))  # ICONDIR

    offset = 6 + 16 * len(sizes)
    for size, data in zip(sizes, images):
        out += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,  # 256 用 0 表示
            0 if size >= 256 else size,
            0,   # 调色板颜色数
            0,   # 保留
            1,   # 色彩平面
            32,  # 位深
            len(data),
            offset,
        )
        offset += len(data)

    for data in images:
        out += data

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preview", action="store_true",
                        help="额外写一张 256px PNG，方便肉眼确认图形")
    args = parser.parse_args()

    build_ico(OUT)
    print(f"写出 {OUT}")
    print(f"  {len(SIZES)} 个尺寸 {SIZES}")
    print(f"  合计 {OUT.stat().st_size:,} B")

    if args.preview:
        preview = ROOT / "build" / "icon_preview.png"
        write_png(preview, render(256))
        print(f"预览 {preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
