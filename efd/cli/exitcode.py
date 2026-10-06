"""进程退出码。

这三个值是**外壳契约**：构建脚本的冒烟测试、README 的说明、以及用户写的
批处理脚本都可能依赖它们，别随手改数值。

单独立一个模块是因为命令实现（``commands``）和分发器（``runner``）都要用到，
而两者不能互相 import。
"""

from __future__ import annotations

EXIT_OK = 0
"""一切正常。"""

EXIT_ERROR = 1
"""出错了：接口结构变了、CDN 不支持 Range、或系统 IO 失败。"""

EXIT_PARTIAL = 2
"""被打断（Ctrl+C）——已装好的文件保留，重新运行会续传。"""

__all__ = ["EXIT_ERROR", "EXIT_OK", "EXIT_PARTIAL"]
