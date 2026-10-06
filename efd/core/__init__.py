"""纯粹的逻辑层：配置、工具、探测、偏好、限速、清单、计划、安装。

这一层不依赖网络实现，也不依赖任何 UI——可以被 CLI、GUI 和测试直接调用。
"""

from __future__ import annotations
