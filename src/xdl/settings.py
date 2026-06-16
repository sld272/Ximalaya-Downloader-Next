# -*- coding: utf-8 -*-
"""用户配置（见 docs/architecture.md §9）。

与平台数据化配置（config/）隔离：这里是用户数据，升级不应覆盖。
MVP 给保守默认值；环境变量/命令行覆盖等留待后续。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _default_auth_path() -> str:
    base = os.environ.get("XDL_HOME") or os.path.join(os.path.expanduser("~"), ".xdl")
    return os.path.join(base, "auth.json")


@dataclass
class Settings:
    download_dir: str = "./downloads"
    default_quality: str = "standard"
    resolve_timeout: int = 40       # 解析（捕获 baseInfo）超时（秒）
    http_timeout: int = 60          # 下载超时（秒）
    auth_path: str = ""

    def __post_init__(self):
        if not self.auth_path:
            self.auth_path = _default_auth_path()
