# -*- coding: utf-8 -*-
"""用户配置（见 docs/architecture.md §9）。

与平台数据化配置（config/）隔离：这里是用户数据，升级不应覆盖。
MVP 给保守默认值；环境变量/命令行覆盖等留待后续。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .config import platform
from .config.paths import xdl_home
from .errors import ConfigError


def _xdl_home() -> str:
    """兼容旧的内部导入路径；新代码使用 ``config.paths.xdl_home``。"""
    return xdl_home()


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
    # 默认无头静默运行；遇到需要人工完成的站点交互时会停止当前批次。
    chrome_headless: bool = True
    # 仅在检测到需要人工完成的站点验证时显示浏览器窗口；否则立即熔断当前批次。
    risk_fallback_headful: bool = True
    # 旧设备状态重置实验可能破坏站点会话的辅助存储，默认关闭，且登录流程绝不调用它。
    # 它不是认证或风控恢复手段；若保留作历史诊断，只能显式启用并自行验证结果。
    reset_device_fingerprint: bool = False

    # 受保护播放信息解析默认串行；2026-07-11 实测 K=4 已触发 3005。
    max_concurrency: int = 1

    # 错误分级退避重试（见 errors.py / 架构 §8.3）
    max_attempts: int = 3              # 单任务即时重试上限
    retry_backoff_base: float = 1.5    # 网络/签名类退避基数（秒）
    cooldown: float = 30.0             # 限流(1001)类冷却（秒）
    global_retry_rounds: int = 2       # 失败收尾轮数

    # ---- 在线音源后端（见 architecture §7.1/§7.2） ----
    # "http"（默认：PySignProvider 本地生成 xm-sign，复用已登录 Cookie）
    # "chrome"（兼容后端：由浏览器页面完成请求，仅用于回退与诊断）
    source_backend: str = "http"
    # xm-sign 设备指纹 JSON（默认 ~/.xdl/device-info.json）；不存在时回退到内置模板。
    device_info_path: str = ""
    # 从 Chrome profile 提取的登录 Cookie 缓存（默认 ~/.xdl/cookies.json）。
    cookies_cache_path: str = ""
    # curl-cffi 的可选传输配置（仅 `source_backend = "http"` 下生效）。它不建立
    # 授权，也不保证平台接受请求；认证与风险响应始终由调用方处理。
    source_impersonate: str = "chrome146"

    def __post_init__(self):
        if self.max_concurrency < 1:
            raise ConfigError("异步并发数必须是大于 0 的整数。")
        if not self.chrome_profile_dir:
            self.chrome_profile_dir = os.path.join(_xdl_home(), "chrome-profile")
        if not self.task_db_path:
            self.task_db_path = os.path.join(_xdl_home(), "tasks.db")
        if not self.risk_log_path:
            self.risk_log_path = os.path.join(_xdl_home(), "risk-events.jsonl")
        if not self.chrome_path:
            self.chrome_path = platform.find_chrome() or ""
        from .config import sign as sign_conf
        if not self.device_info_path:
            self.device_info_path = sign_conf.default_device_info_path()
        if not self.cookies_cache_path:
            self.cookies_cache_path = sign_conf.default_cookies_cache_path()
