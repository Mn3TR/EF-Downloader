"""PyInstaller 的入口脚本。

不能直接把 ``efd/__main__.py`` 指给 PyInstaller —— 那个文件用的是相对导入
（``from .cli import main``），被当作顶层脚本执行时会失败。
"""

from __future__ import annotations

import sys

from efd.cli import main

if __name__ == "__main__":
    sys.exit(main())
