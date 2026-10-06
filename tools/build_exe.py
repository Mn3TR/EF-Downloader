"""构建单文件 exe。

    python tools/build_exe.py --setup      # 一次性：建构建 venv 并装 PyInstaller
    python tools/build_exe.py              # 构建到 dist/EFD.exe
    python tools/build_exe.py --verify     # 构建 + 冒烟测试（CLI 与 GUI 两条路径）
    python tools/build_exe.py --clean      # 先清掉 build/ 与 dist/

两个刻意的设计：

* **不碰你的全局 Python。** 优先用仓库内的 ``.venv-build``；没有就提示
  ``--setup``，而不是偷偷 pip install 到系统环境。
* **把 PyInstaller 的缓存挪出 C 盘。** 它默认写 ``%LOCALAPPDATA%``，
  而这台机器 C 盘只剩不到 1 GB。缓存改到 ``build/`` 下。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

VENV = ROOT / ".venv-build"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
SPEC = ROOT / "packaging" / "efd.spec"
EXE = DIST / "EFD.exe"
PYINSTALLER_REQ = "pyinstaller>=6.0"


# ---------------------------------------------------------------- 工具链


def _venv_python() -> Path:
    return VENV / "Scripts" / "python.exe"


def _has_pyinstaller(python: Path) -> bool:
    if not python.exists():
        return False
    try:
        return subprocess.run(
            [str(python), "-c", "import PyInstaller"],
            capture_output=True, timeout=180,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def setup_venv() -> Path:
    python = _venv_python()
    if not python.exists():
        print(f"[setup] 创建构建 venv -> {VENV}")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    print(f"[setup] 安装 {PYINSTALLER_REQ}（只装进这个 venv）")
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "--upgrade", PYINSTALLER_REQ],
        check=True,
    )
    return python


def pick_toolchain(auto_setup: bool) -> Path:
    python = _venv_python()
    if _has_pyinstaller(python):
        return python
    if _has_pyinstaller(Path(sys.executable)):
        return Path(sys.executable)
    if auto_setup:
        return setup_venv()
    raise SystemExit(
        "找不到 PyInstaller。先跑：\n"
        "    python tools/build_exe.py --setup\n"
        f"（它会建 {VENV.name}/ 并只在那里安装，不动你的全局 Python）"
    )


# ---------------------------------------------------------------- 构建


def build(python: Path, clean: bool) -> Path:
    if clean:
        for path in (DIST, BUILD):
            shutil.rmtree(path, ignore_errors=True)

    cache = BUILD / "pyinstaller-cache"
    cache.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYINSTALLER_CONFIG_DIR"] = str(cache)
    env["PYTHONIOENCODING"] = "utf-8"

    cmd = [
        str(python), "-m", "PyInstaller", str(SPEC),
        "--noconfirm",
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "pyinstaller"),
    ]
    print("[build] " + " ".join(cmd))
    subprocess.run(cmd, check=True, env=env, cwd=ROOT)
    return EXE


def report(exe: Path) -> None:
    size = exe.stat().st_size
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    print(f"\n产物   {exe}")
    print(f"体积   {size:,} B  ({size / 2**20:.1f} MiB)")
    print(f"sha256 {digest}")


# ---------------------------------------------------------------- 冒烟测试


def _all_window_titles() -> list[str]:
    """枚举所有顶层窗口标题。"""
    user32 = ctypes.windll.user32
    titles: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):  # pragma: no cover - Windows 回调
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            titles.append(buf.value)
        return True

    user32.EnumWindows(callback, 0)
    return titles


def _kill_tree(pid: int) -> None:
    """连子进程一起杀。

    onefile exe 的 bootloader 会 ``CreateProcess`` 出一个子进程来跑真正的应用，
    只杀父进程会留下一个没有窗口的孤儿。
    """
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, check=False)


def verify(exe: Path) -> bool:
    from efd.ui.theme import APP_TITLE

    print("\n[verify] 1/3 命令行路径（--version）")
    # 必须显式指定 UTF-8：efd 在管道场景会写 UTF-8（见 efd.cli._setup_output_encoding），
    # 而 subprocess 的 text=True 默认按本地代码页（简中 gbk）解码，会直接抛
    # UnicodeDecodeError。这里踩过一次，别再改回去。
    result = subprocess.run([str(exe), "--version"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=300)
    got = (result.stdout or "").strip()
    cli_ok = result.returncode == 0 and got.startswith("efd ")
    print(f"         -> {got!r}  exit={result.returncode}  {'OK' if cli_ok else 'FAIL'}")

    print("[verify] 2/3 命令行路径（--help 子命令齐全）")
    result = subprocess.run([str(exe), "--help"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=300)
    subcommands = all(name in (result.stdout or "")
                      for name in ("plan", "install", "probe", "gui"))
    print(f"         -> plan/install/probe/gui 全部出现：{subcommands}")

    print("[verify] 3/3 图形界面路径（无参数 = 模拟双击）")
    proc = subprocess.Popen([str(exe)])
    gui_ok = False
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            time.sleep(0.5)
            if APP_TITLE in _all_window_titles():
                gui_ok = True
                break
            if proc.poll() is not None:
                break
        print(f"         -> 窗口 {APP_TITLE!r} 出现：{gui_ok}")
    finally:
        _kill_tree(proc.pid)
        time.sleep(0.5)

    leftovers = _all_window_titles().count(APP_TITLE)
    print(f"         关闭后残留窗口：{leftovers}")

    ok = cli_ok and subcommands and gui_ok and leftovers == 0
    print(f"\n[verify] {'全部通过' if ok else '有失败项'}")
    return ok


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    from efd.core.util import setup_output_encoding

    # 和 CLI 用同一套：管道/重定向时写 UTF-8，免得本脚本的中文在
    # PowerShell 7 / CI 里变成 U+FFFD。
    setup_output_encoding()

    parser = argparse.ArgumentParser(prog="build_exe", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--setup", action="store_true", help="建构建 venv 并安装 PyInstaller")
    parser.add_argument("--clean", action="store_true", help="构建前清掉 build/ 与 dist/")
    parser.add_argument("--verify", action="store_true", help="构建后跑冒烟测试")
    parser.add_argument("--skip-build", action="store_true", help="只用现有的 dist/EFD.exe 做验证")
    args = parser.parse_args(argv)

    if args.setup:
        setup_venv()
        if not args.verify and not args.skip_build:
            print("\nvenv 就绪。现在可以跑：python tools/build_exe.py")
            return 0

    if not args.skip_build:
        exe = build(pick_toolchain(auto_setup=True), clean=args.clean)
        report(exe)
    else:
        exe = EXE
        if not exe.exists():
            raise SystemExit(f"{exe} 不存在，去掉 --skip-build 重新构建")

    if args.verify:
        return 0 if verify(exe) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
