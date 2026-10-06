"""命令分发与顶层错误翻译。

这一层是**唯一**把异常变成退出码的地方：各个 ``cmd_*`` 只管抛，用户看到的
中文说明在这里统一生成。
"""

from __future__ import annotations

import sys

from ..core.seed import SeedError
from ..core.util import setup_output_encoding
from ..net import RangeError
from .commands import cmd_gui, cmd_install, cmd_plan, cmd_probe
from .exitcode import EXIT_ERROR, EXIT_PARTIAL
from .parser import build_parser

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


__all__ = ["COMMANDS", "main"]
