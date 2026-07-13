# -*- coding: utf-8 -*-
"""XDL 用户数据目录。

放在配置层的叶子模块中，避免 `settings` 与平台配置互相导入。
"""
from __future__ import annotations

import os


def xdl_home() -> str:
    """返回用户数据目录；可用 ``XDL_HOME`` 覆盖。"""
    return os.environ.get("XDL_HOME") or os.path.join(
        os.path.expanduser("~"), ".xdl"
    )
