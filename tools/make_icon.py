"""从 ``ico.png`` 生成 exe 用的图标。

    python tools/make_icon.py --preview

源图是 1254×1254 的深色底 + 白色字标，字标本身是 3.2:1 的横排。直接塞进方形
图标会留下大片空地、小尺寸下更是一团糊。所以按尺寸分两档：

    16 / 32 / 48   —— 只用「EF」，笔画粗，缩到 16px 仍然认得出
    ≥ 64           —— 完整的「EF / DOWNLOADER」锁定组合

全部用标准库实现（``zlib`` 解 PNG，手写 ICO），不引入 Pillow —— 这个项目
零依赖是条底线，构建期也一样。

产物 ``packaging/efd.ico`` 会入库，重新构建 exe 不需要先跑这个脚本。
"""

from __future__ import annotations

import struct
import sys
import zlib
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "ico.png"
OUT = ROOT / "packaging" / "efd.ico"

SIZES = (16, 32, 48, 64, 128, 256)

#: 小于这个尺寸只画「EF」，否则画完整锁定组合。
LOCKUP_FROM = 64

#: 底色，与源图背景一致。
TILE = (0x19, 0x19, 0x19)
#: 圆角半径（占边长比例）。
CORNER = 0.22
#: 字标占图标边长的比例。
MARK_FILL = 0.80
#: 锁定组合是横条，可以铺得宽一些。
LOCKUP_FILL = 0.88

#: 源图背景亮度、字标墨色亮度（实测值）。
BG_LUM = 25
INK_LUM = 230
#: 低于这个覆盖度当作背景噪声抹掉。
DEAD_ZONE = 0.03

SS = 4  # 圆角遮罩的超采样倍数


# ------------------------------------------------------------------ PNG


def _decode_png(path: Path) -> tuple[int, int, int, bytes]:
    """解出 8 位 PNG，返回 (宽, 高, 通道数, RGB(A) 原始字节)。"""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} 不是 PNG")

    off, idat = 8, bytearray()
    width = height = depth = color = None
    while off < len(data):
        length = struct.unpack(">I", data[off:off + 4])[0]
        tag = data[off + 4:off + 8]
        body = data[off + 8:off + 8 + length]
        if tag == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or interlace != 0:
                raise ValueError(f"只支持 8 位非隔行 PNG，这里是 depth={depth} interlace={interlace}")
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        off += 12 + length

    if color not in (0, 2, 4, 6):
        raise ValueError(f"不支持的 PNG 颜色类型 {color}")
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[color]

    raw = zlib.decompress(bytes(idat))
    stride = width * ch
    out = bytearray(stride * height)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ftype == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if (pa <= pb and pa <= pc) else (b if pb <= pc else c))) & 0xFF
        elif ftype != 0:
            raise ValueError(f"未知 PNG 行过滤器 {ftype}")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, ch, bytes(out)


# ------------------------------------------------------------- 字标提取


def _coverage(width: int, height: int, ch: int, px: bytes) -> array:
    """把「白字 + 深底」变成覆盖度图：底=0，字=1，抗锯齿边缘是中间值。

    源图是不透明 RGB，没有 alpha 通道；但字是白的、底是暗的，亮度本身
    就是覆盖度。这样提取出来的字标是干净的白色透明蒙版，可以叠到任意
    底色上，边缘的抗锯齿也原样保留。
    """
    span = INK_LUM - BG_LUM
    cov = array("f", bytes(4 * width * height))
    for y in range(height):
        base = y * width * ch
        row = y * width
        for x in range(width):
            o = base + x * ch
            lum = (px[o] * 299 + px[o + 1] * 587 + px[o + 2] * 114) // 1000
            v = (lum - BG_LUM) / span
            if v <= DEAD_ZONE:
                continue
            cov[row + x] = 1.0 if v >= 1.0 else v
    return cov


def _row_spans(cov: array, width: int, height: int, thr: float = 0.06):
    """每行的 [最左, 最右, 像素数]，没有内容则 (None, None, 0)。"""
    spans = []
    for y in range(height):
        base = y * width
        lo = hi = None
        n = 0
        for x in range(width):
            if cov[base + x] > thr:
                if lo is None:
                    lo = x
                hi = x
                n += 1
        spans.append((lo, hi, n))
    return spans


def _bbox(spans, y_from: int, y_to: int):
    lo = hi = None
    for y in range(y_from, y_to + 1):
        a, b, n = spans[y]
        if not n:
            continue
        lo = a if lo is None else min(lo, a)
        hi = b if hi is None else max(hi, b)
    if lo is None:
        raise ValueError("这段区域里没有内容")
    return lo, hi


def _regions(cov: array, width: int, height: int):
    """切出「EF」和「EF + DOWNLOADER」两块，返回 (x0, y0, x1, y1) 闭区间。"""
    spans = _row_spans(cov, width, height)
    rows = [y for y, s in enumerate(spans) if s[2]]
    if not rows:
        raise ValueError("源图里找不到任何内容")
    top, bottom = rows[0], rows[-1]

    whole_x0, whole_x1 = _bbox(spans, top, bottom)
    # DOWNLOADER 那一行会一直伸到接近全宽，「EF」只占左边一小半；
    # 抓最右端的第一次跳变就是两带的分界。
    wide = whole_x0 + (whole_x1 - whole_x0) * 0.7
    split = bottom + 1
    for y in range(top, bottom + 1):
        if spans[y][1] is not None and spans[y][1] >= wide:
            split = y
            break
    if split > bottom:
        raise ValueError("没找到 EF 与 DOWNLOADER 的分界")

    ef_x0, ef_x1 = _bbox(spans, top, split - 1)
    down_y0 = split
    while down_y0 <= bottom and not spans[down_y0][2]:
        down_y0 += 1
    dl_x0, dl_x1 = _bbox(spans, down_y0, bottom)

    mark = (ef_x0, top, ef_x1, split - 1)
    lockup = (min(ef_x0, dl_x0), top, max(ef_x1, dl_x1), bottom)
    return mark, lockup


# --------------------------------------------------------------- 重采样


def _mip(region: array, width: int, height: int, min_width: int = 96):
    """逐级 2× 盒式缩小的金字塔，采样时取分辨率刚好够用的那一层。"""
    levels = [(region, width, height)]
    while width > min_width and height > 8:
        nw, nh = max(1, width // 2), max(1, height // 2)
        src, sw, sh = levels[-1]
        dst = array("f", bytes(4 * nw * nh))
        for j in range(nh):
            y0 = (j * sh) // nh
            y1 = max(y0 + 1, ((j + 1) * sh) // nh)
            for i in range(nw):
                x0 = (i * sw) // nw
                x1 = max(x0 + 1, ((i + 1) * sw) // nw)
                s = 0.0
                for y in range(y0, y1):
                    b = y * sw
                    for x in range(x0, x1):
                        s += src[b + x]
                dst[j * nw + i] = s / ((y1 - y0) * (x1 - x0))
        levels.append((dst, nw, nh))
        width, height = nw, nh
    return levels


def _pick(levels, want: int):
    """挑一层宽度 >= want 的最小层；都不够就用最原始那层。"""
    best = levels[0]
    for lv in levels:
        if lv[1] >= want:
            best = lv
        else:
            break
    return best


def _resample(cropped, out_w: int, out_h: int) -> array:
    """把已裁好的区块按面积平均缩到 out_w×out_h。"""
    levels = _mip(*cropped)
    src, sw, sh = _pick(levels, out_w)
    out = array("f", bytes(4 * out_w * out_h))
    for j in range(out_h):
        sy0 = (j * sh) / out_h
        sy1 = ((j + 1) * sh) / out_h
        ja, jb = int(sy0), min(sh, max(int(sy0) + 1, int(sy1 + 0.999999)))
        for i in range(out_w):
            sx0 = (i * sw) / out_w
            sx1 = ((i + 1) * sw) / out_w
            ia, ib = int(sx0), min(sw, max(int(sx0) + 1, int(sx1 + 0.999999)))
            s = 0.0
            for y in range(ja, jb):
                b = y * sw
                for x in range(ia, ib):
                    s += src[b + x]
            out[j * out_w + i] = s / ((jb - ja) * (ib - ia))
    return out


def _crop(region: array, width: int, height: int, box):
    x0, y0, x1, y1 = box
    w, h = x1 - x0 + 1, y1 - y0 + 1
    out = array("f", bytes(4 * w * h))
    for j in range(h):
        sb = (y0 + j) * width + x0
        out[j * w:(j + 1) * w] = region[sb:sb + w]
    return out, w, h


# ----------------------------------------------------------- 圆角遮罩


_MASTER_PX = 1024
_MASTER_MASK: bytearray | None = None


def _master_mask() -> bytearray:
    """1024×1024 的圆角方块二值遮罩，整个进程只算一次。"""
    global _MASTER_MASK
    if _MASTER_MASK is None:
        n = _MASTER_PX
        hi = bytearray(n * n)
        r = CORNER
        for j in range(n):
            y = (j + 0.5) / n
            dy = y - min(max(y, r), 1 - r)
            base = j * n
            for i in range(n):
                x = (i + 0.5) / n
                dx = x - min(max(x, r), 1 - r)
                if dx * dx + dy * dy <= r * r:
                    hi[base + i] = 255
        _MASTER_MASK = hi
    return _MASTER_MASK


def _mask_at(size: int) -> array:
    """size×size 的圆角方块覆盖度（0..1，边缘抗锯齿）。"""
    master = _MASTER_PX
    hi = _master_mask()
    if size >= master:
        return array("f", (v / 255.0 for v in hi))
    out = array("f", bytes(4 * size * size))
    for j in range(size):
        y0 = (j * master) // size
        y1 = max(y0 + 1, ((j + 1) * master) // size)
        for i in range(size):
            x0 = (i * master) // size
            x1 = max(x0 + 1, ((i + 1) * master) // size)
            s = 0
            for y in range(y0, y1):
                b = y * master
                s += sum(hi[b + x0:b + x1])
            out[j * size + i] = s / ((y1 - y0) * (x1 - x0) * 255.0)
    return out


# ------------------------------------------------------------- 合成 / ICO


def render(size: int, cropped, boxes) -> list[list[tuple[int, int, int, int]]]:
    """渲染一张 size×size RGBA 图标（行优先，自上而下）。

    ``cropped`` 是 ``_crop`` 出来的 (像素, 宽, 高)，``boxes`` 是对应的源图包围盒。
    """
    use_lockup = size >= LOCKUP_FROM
    idx = 1 if use_lockup else 0
    box = boxes[idx]
    region = cropped[idx]
    fill = LOCKUP_FILL if use_lockup else MARK_FILL

    src_w = box[2] - box[0] + 1
    src_h = box[3] - box[1] + 1

    inner = size * fill
    scale = min(inner / src_w, inner / src_h)
    bw = max(1, round(src_w * scale))
    bh = max(1, round(src_h * scale))
    ox, oy = (size - bw) // 2, (size - bh) // 2

    mark = _resample(region, bw, bh)
    mask = _mask_at(size)

    tr, tg, tb = TILE
    pixels = []
    for j in range(size):
        row = []
        mrow = j * size
        for i in range(size):
            a = mask[mrow + i]
            if a <= 0.0:
                row.append((0, 0, 0, 0))
                continue
            yy, xx = j - oy, i - ox
            c = mark[yy * bw + xx] if (0 <= yy < bh and 0 <= xx < bw) else 0.0
            r = round(tr + (255 - tr) * c)
            g = round(tg + (255 - tg) * c)
            b = round(tb + (255 - tb) * c)
            row.append((r, g, b, round(a * 255)))
        pixels.append(row)
    return pixels


def _dib(pixels) -> bytes:
    """BITMAPINFOHEADER + BGRA 位图 + AND 掩码（ICO 里的标准 DIB 形态）。"""
    size = len(pixels)
    header = struct.pack(
        "<IiiHHIIiiII",
        40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0,
    )
    xor = bytearray()
    for row in reversed(pixels):  # DIB 自下而上
        for r, g, b, a in row:
            xor += bytes((b, g, r, a))
    stride = ((size + 31) // 32) * 4
    return header + bytes(xor) + bytes(stride * size)


def build_ico(path: Path, frames) -> None:
    out = bytearray()
    out += struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    for size, data in frames:
        out += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0, 0, 1, 32, len(data), offset,
        )
        offset += len(data)
    for _, data in frames:
        out += data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


def write_png(path: Path, pixels) -> None:
    """把 RGBA 像素写成 PNG。只用于人工预览，不进 exe。"""
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


def _contact_sheet(frames, scale: int, bg=(0x2E, 0x2E, 0x36)) -> list:
    """把各尺寸图标并排拼一张图，方便肉眼验收。"""
    pad = 12
    width = pad
    for size, _ in frames:
        width += size * scale + pad
    height = max(size for size, _ in frames) * scale + pad * 2
    sheet = [[(*bg, 255)] * width for _ in range(height)]
    x = pad
    for size, pixels in frames:
        for j in range(size):
            for i in range(size):
                r, g, b, a = pixels[j][i]
                if a == 0:
                    continue
                ox = x + i * scale
                for dy in range(scale):
                    row = sheet[pad + j * scale + dy]
                    for dx in range(scale):
                        row[ox + dx] = (r, g, b, 255)
        x += size * scale + pad
    return sheet


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preview", action="store_true", help="额外写预览图，方便肉眼确认")
    parser.add_argument("--source", type=Path, default=SRC, help="源图，默认 ico.png")
    args = parser.parse_args()

    w, h, ch, px = _decode_png(args.source)
    print(f"读入 {args.source.name}  {w}×{h}  {ch} 通道")

    cov = _coverage(w, h, ch, px)
    mark_box, lockup_box = _regions(cov, w, h)
    for name, b in (("EF", mark_box), ("EF+DOWNLOADER", lockup_box)):
        print(f"  {name:<14} x[{b[0]},{b[2]}] y[{b[1]},{b[3]}] -> "
              f"{b[2]-b[0]+1}×{b[3]-b[1]+1}")

    regions = [_crop(cov, w, h, mark_box), _crop(cov, w, h, lockup_box)]
    frames = []
    for size in SIZES:
        frames.append((size, render(size, regions, (mark_box, lockup_box))))

    build_ico(OUT, [(s, _dib(p)) for s, p in frames])
    print(f"写出 {OUT}")
    print(f"  {len(SIZES)} 个尺寸 {SIZES}")
    print(f"  合计 {OUT.stat().st_size:,} B")

    if args.preview:
        sheet = ROOT / "build" / "icon_sizes.png"
        write_png(sheet, _contact_sheet(frames, 1))
        big = ROOT / "build" / "icon_zoom.png"
        write_png(big, _contact_sheet([(s, p) for s, p in frames if s <= 48], 5))
        huge = ROOT / "build" / "icon_256.png"
        write_png(huge, render(256, regions, (mark_box, lockup_box)))
        print(f"预览 {sheet}")
        print(f"预览 {big}")
        print(f"预览 {huge}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
