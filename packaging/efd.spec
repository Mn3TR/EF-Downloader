# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包定义。

    pyinstaller packaging/efd.spec --noconfirm --distpath dist --workpath build

通常不用直接敲上面这行，用 ``python tools/build_exe.py`` 即可
（它还会把 PyInstaller 的缓存从 %LOCALAPPDATA% 挪走，并做冒烟测试）。

产物是**单个控制台模式 exe**，三种用法都成立：

  * 双击（无参数）        → 直接进图形界面
  * 终端 `EFD.exe plan …` → 正常命令行输出
  * `EFD.exe gui`         → 由 efd.gui.detach_console() 摘掉控制台窗口

为什么不用 ``--noconsole``：那会把 CLI 的输出一起干掉。控制台模式 +
FreeConsole 能同时满足两种用法，而且**只需要一个文件**。
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(SPECPATH))  # noqa: F821 - PyInstaller 注入
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from efd import __version__  # noqa: E402

ICON = os.path.join(ROOT, "packaging", "efd.ico")


def _version_tuple(text: str) -> tuple:
    """'0.1.0' -> (0, 1, 0, 0)。Windows 版本资源要求四段数字。"""
    parts = []
    for chunk in text.split(".")[:4]:
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (4 - len(parts)))


# exe 的「右键属性 → 详细信息」内容。版本号**只从 efd.__version__ 来**，
# 这个文件是生成物，不新增第二处真相。
# 没有它的话属性面板一片空白，看起来像来路不明的程序，部分杀软启发式也吃这一套。
VERSION_FILE = os.path.join(ROOT, "build", "version_info.txt")
os.makedirs(os.path.dirname(VERSION_FILE), exist_ok=True)
_V = _version_tuple(__version__)
with open(VERSION_FILE, "w", encoding="utf-8") as _f:
    _f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_V}, prodvers={_V},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'Mn3TR'),
        StringStruct('FileDescription', 'EF DOWNLOADER'),
        StringStruct('FileVersion', '{__version__}'),
        StringStruct('InternalName', 'EFD'),
        StringStruct('LegalCopyright', 'Copyright (c) 2026 Mn3TR. MIT License.'),
        StringStruct('OriginalFilename', 'EFD.exe'),
        StringStruct('ProductName', 'EF DOWNLOADER'),
        StringStruct('ProductVersion', '{__version__}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""")

# 运行时用不到，剔掉能明显减小体积。
EXCLUDES = [
    "unittest", "doctest", "pydoc", "pdb", "lib2to3", "distutils",
    "setuptools", "pip", "wheel", "pkg_resources",
    "pytest", "numpy", "PIL", "tkinter.test",
]

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "packaging", "entry.py")],
    pathex=[ROOT],
    binaries=[],
    # 刻意留空：efd 包没有任何运行时数据文件（33 KB 的 wheel 就是证据），
    # data/ 下的 6.7 MB 夹具只服务于测试，不该进 exe。
    datas=[],
    # efd.cli 里 gui 是函数内导入；显式列出来，保证 tkinter 一定被打进去。
    hiddenimports=["efd.gui"],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="EFD",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version=VERSION_FILE,
)
