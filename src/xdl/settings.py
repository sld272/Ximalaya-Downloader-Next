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
    risk_log_path: str = ""         # 风控观测 JSONL（默认 ~/.xdl/risk-events.jsonl）
    cdp_port: int = 9222            # Chrome 远程调试端口
    # 默认无头静默运行（不弹窗）。无头会带 "HeadlessChrome" UA 这一自动化指纹，适配器
    # 会在解析时通过 CDP 把它抹成正常 "Chrome" 以降低风控识别（见 source_chrome）。
    chrome_headless: bool = True
    # 仅在检测到图形验证码风控时，自动切到有头浏览器让用户手动通过一次，过完再回无头。
    # headless 无法人工过验证码，这是被惩罚后唯一能恢复的路径。置 False 则遇验证码直接熔断。
    risk_fallback_headful: bool = True
    # 是否在每次会话启动/登录后重置设备指纹 Cookie（_xmLog / wfp / Hm_lvt_*），保留登录态。
    # 喜马拉雅的设备风控跟 _xmLog/wfp 这一组设备标识走，不跟账号走（用户日常浏览器同账号
    # 无风控已证明）。清除后页面 SDK 会为该 Profile 重新生成新设备 ID，等同"在新设备登录"，
    # 摆脱旧设备上累积的验证码惩罚态。置 False 可做 A/B 对照。见 docs/risk-control-observations.md。
    reset_device_fingerprint: bool = True

    # 受保护播放信息解析默认串行；2026-07-11 实测 K=4 已触发 3005。
    max_concurrency: int = 1

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
        if not self.risk_log_path:
            self.risk_log_path = os.path.join(_xdl_home(), "risk-events.jsonl")
        if not self.chrome_path:
            self.chrome_path = platform.find_chrome() or ""
