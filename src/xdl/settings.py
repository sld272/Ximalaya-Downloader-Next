# -*- coding: utf-8 -*-
"""用户配置（见 docs/architecture.md §9）。

与平台数据化配置（config/）隔离：这里是用户数据，升级不应覆盖。
MVP 给保守默认值；环境变量/命令行覆盖等留待后续。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .config import platform


def _xdl_home() -> str:
    return os.environ.get("XDL_HOME") or os.path.join(os.path.expanduser("~"), ".xdl")


@dataclass
class Settings:
    download_dir: str = "./downloads"
    default_quality: str = "standard"
    resolve_timeout: int = 40       # 解析（捕获 baseInfo）超时（秒）
    http_timeout: int = 60          # 下载超时（秒）

    # 真实 Chrome 接管（见 adapters/source_chrome.py）
    chrome_path: str = ""           # 为空则自动探测
    chrome_profile_dir: str = ""    # 专用 Chrome 用户配置目录（持久化登录态）
    task_db_path: str = ""          # 任务库（默认 ~/.xdl/tasks.db）
    cdp_port: int = 9222            # Chrome 远程调试端口
    chrome_headless: bool = True    # 下载解析用无头真实 Chrome（登录始终有头）

    # 并发：专辑解析下载的并发上界（探测显示 K≤6 不触发频率风控，默认保守取 4）
    max_concurrency: int = 4

    # 错误分级退避重试（见 errors.py / 架构 §8.3）
    max_attempts: int = 3              # 单任务即时重试上限
    retry_backoff_base: float = 1.5    # 网络/签名类退避基数（秒）
    cooldown: float = 30.0             # 限流(1001)类冷却（秒）
    global_retry_rounds: int = 2       # 失败收尾轮数

    def __post_init__(self):
        if not self.chrome_profile_dir:
            self.chrome_profile_dir = os.path.join(_xdl_home(), "chrome-profile")
        if not self.task_db_path:
            self.task_db_path = os.path.join(_xdl_home(), "tasks.db")
        if not self.chrome_path:
            self.chrome_path = platform.find_chrome() or ""
