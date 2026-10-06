"""命令行层。

    efd/cli/
        exitcode.py  进程退出码（外壳契约）
        parser.py    参数定义与解析
        report.py    归档/计划的人读渲染（只读）
        commands.py  四个子命令的编排
        runner.py    分发 + 顶层错误翻译（唯一决定退出码的地方）
        __init__.py  对外只暴露 main / build_parser，其余按需取

``main`` 与 ``build_parser`` 是**公开入口**（``packaging/entry.py`` 与构建脚本
的冒烟测试直接引用）；带下划线的名字是内部细节，但测试会按名引用它们。
"""

from __future__ import annotations

from .exitcode import EXIT_ERROR, EXIT_OK, EXIT_PARTIAL
from .parser import (
    _add_scope_args,
    _resolve_excludes,
    _resolve_netopts,
    _resolve_target,
    build_parser,
)
from .runner import COMMANDS, main

__all__ = ["build_parser", "main"]
