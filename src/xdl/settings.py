# -*- coding: utf-8 -*-
"""用户配置（见 docs/architecture.md §9）。

与平台数据化配置（config/）隔离：这里是用户数据，升级不应覆盖。
MVP 给保守默认值；环境变量/命令行覆盖等留待后续。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from .config import paths, platform
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

    # 浏览器选择（登录/Cookie 导出/设备指纹采集所用的真实浏览器）。
    # "auto" = Chrome 优先、Edge 兜底；显式 "chrome"/"edge" 只用指定浏览器。
    browser: str = "auto"

    # 真实浏览器接管（见 adapters/source_chrome.py；Chrome 与 Edge 同为 Chromium）
    chrome_path: str = ""           # 显式可执行文件路径；为空则按 browser 自动探测
    # 专用浏览器用户配置目录（持久化登录态；默认 ~/.xdl/{browser}-profile）
    chrome_profile_dir: str = ""
    task_db_path: str = ""          # 任务库（默认 ~/.xdl/tasks.db）
    risk_log_path: str = ""         # 风控观测 JSONL（默认 ~/.xdl/risk-events.jsonl）
    cdp_port: int = 9222            # 浏览器远程调试端口
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
    # "pc"（PC 桌面端接口：play/v1/show + track/quality，解析链路更简单）
    # "chrome"（兼容后端：由浏览器页面完成请求，仅用于回退与诊断）
    source_backend: str = "http"

    # ---- APK 9.5.1.3 协议后端（仅 source_backend="apk" 时使用）----
    apk_state_dir: str = ""       # 默认 ~/.xdl/apk
    apk_java_path: str = "java"
    apk_signer_jar: str = ""
    apk_libcxx_path: str = ""
    apk_login_so_path: str = ""
    apk_xuid_so_path: str = ""
    apk_encrypt_so_path: str = ""
    apk_asset_dir: str = ""
    apk_request_timeout: float = 30.0
    apk_native_timeout: float = 30.0
    apk_max_consecutive_failures: int = 3
    # xm-sign 设备指纹 JSON（默认 ~/.xdl/{browser}-device-info.json）；
    # 不存在时回退到内置模板。
    device_info_path: str = ""
    # 从浏览器 Profile 提取的登录 Cookie 缓存（默认 ~/.xdl/{browser}-cookies.json）。
    cookies_cache_path: str = ""
    # curl-cffi 的可选传输配置（仅 `source_backend = "http"` 下生效）。它不建立
    # 授权，也不保证平台接受请求；认证与风险响应始终由调用方处理。
    # curl-cffi 本机实测可用的最新桌面 Chrome 伪装；chrome150 在当前 curl_cffi 上不支持。
    # device_info 的 UA 可与本机 Chrome 大版本一致（如 150），TLS 指纹走这里。
    source_impersonate: str = "chrome146"

    # ---- 风控后刷新设备指纹（实验，默认关闭）----
    # 命中已识别风控后，用真实浏览器重生 device_info 并重试当前曲。
    # 不保证服务端接受，也不是默认抗风控策略。
    experiment_rotate_device_on_risk: bool = False
    experiment_browser_clear_state: bool = True
    # 更真实的换身：临时全新 Profile + 只注入登录 Cookie（默认开）。
    experiment_browser_fresh_profile: bool = True
    # 换身采集默认有头；headless 会在 device_info 写入 HeadlessChrome。
    # None = 跟随 chrome_headless；True/False 强制。
    experiment_rotate_headless: bool | None = False
    experiment_persist_device_info: bool = True
    # False（默认）：换身后保留浏览器导出的设备 Cookie，与新 device_info 成套。
    # True：继续剥掉设备 Cookie，只留登录 token（更激进，也更容易身份平面不一致）。
    experiment_strip_device_cookies: bool = False
    # 换身必须至少改变 session/hardware 身份字段之一，否则视为假换身并中止。
    experiment_require_identity_change: bool = True
    # 清 storage 后的重生轮数（每轮：删设备 Cookie/storage → 新 page 重载）。
    experiment_rebirth_rounds: int = 2
    # 0 = 不限次数（换身后首次成功才允许再换；首次仍风控则本会话停用）
    experiment_max_device_rotations: int = 0
    # 命中风控后、换身/探针前的冷却（秒）。0 = 不等待。
    experiment_risk_cooldown_seconds: float = 15.0

    # ---- 风控后自动恢复（轮询，默认关闭）----
    # 命中已识别风控且任务未完成时，进入「等待 → 单探针 → 解除后继续下载」循环；
    # 等待期间不发任何请求，每个退避周期只发一个探针（真实待下载曲目的 baseInfo）。
    risk_poll_enabled: bool = False
    risk_poll_initial_wait: float = 30.0     # 首次探针前等待（秒）
    risk_poll_backoff_factor: float = 2.0    # 每次仍风控的等待倍增
    risk_poll_max_wait: float = 900.0        # 单次等待上限（秒）
    risk_poll_max_duration: float = 3600.0   # 总轮询上限（秒）；0 = 不限

    def __post_init__(self):
        if self.max_concurrency < 1:
            raise ConfigError("异步并发数必须是大于 0 的整数。")
        self.browser = (self.browser or "auto").strip().lower()
        if self.browser not in platform.BROWSER_CHOICES:
            raise ConfigError(
                f"未知浏览器 {self.browser!r}；可选值为 "
                f"{' / '.join(platform.BROWSER_CHOICES)}。"
            )
        # 显式 chrome_path 优先（浏览器从文件名推断，推断不出按 chrome 处理）；
        # 否则按 browser 偏好自动探测。
        if self.chrome_path:
            resolved = platform.infer_browser_from_path(self.chrome_path) or "chrome"
        else:
            resolved, detected = platform.find_browser(self.browser)
            self.chrome_path = detected or ""
        # 运行时派生结果：不作为 dataclass 字段，asdict/持久化不会收集它。
        self.resolved_browser = resolved
        # 身份三件套（Profile / Cookie 缓存 / 设备指纹）统一按浏览器分家。
        # Profile 必须分：Chromium 系 Cookie 用各浏览器自己的系统凭据存储加密
        # （macOS Keychain / Windows DPAPI + Chrome 127+ 的 App-Bound 加密），
        # 跨浏览器打开同一 Profile 解不开，且启动时会删掉解密失败的 Cookie 行。
        # 另两件是它的派生缓存：Cookie 导出含设备指纹 Cookie、device_info 的 ew1
        # 编码了 UA，跟错 Profile 就会把一个浏览器的会话配上另一个的指纹。
        if not self.chrome_profile_dir:
            self.chrome_profile_dir = paths.browser_profile_dir(resolved)
        if not self.device_info_path:
            self.device_info_path = paths.browser_device_info_path(resolved)
        if not self.cookies_cache_path:
            self.cookies_cache_path = paths.browser_cookies_path(resolved)
        if not self.task_db_path:
            self.task_db_path = os.path.join(_xdl_home(), "tasks.db")
        if not self.risk_log_path:
            self.risk_log_path = os.path.join(_xdl_home(), "risk-events.jsonl")
        if not self.apk_state_dir:
            self.apk_state_dir = os.path.join(_xdl_home(), "apk")
        source_vendor = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..", "vendor", "apk_protocol"
        ))
        installed_vendor = os.path.join(
            sys.prefix, "share", "ximalaya-downloader-next", "apk-protocol"
        )
        vendor = (source_vendor if os.path.isdir(source_vendor)
                  else installed_vendor)
        if not self.apk_signer_jar:
            self.apk_signer_jar = os.path.join(vendor, "native-signer.jar")
        if not self.apk_libcxx_path:
            self.apk_libcxx_path = os.path.join(vendor, "libc++_shared.so")
        if not self.apk_login_so_path:
            self.apk_login_so_path = os.path.join(vendor, "liblogin_encrypt.so")
        if not self.apk_xuid_so_path:
            self.apk_xuid_so_path = os.path.join(vendor, "libnativelib.so")
        if not self.apk_encrypt_so_path:
            self.apk_encrypt_so_path = os.path.join(vendor, "libencrypt.so")
        if not self.apk_asset_dir:
            self.apk_asset_dir = os.path.join(vendor, "assets")
        if self.apk_request_timeout <= 0 or self.apk_native_timeout <= 0:
            raise ConfigError("APK 请求和 native 超时必须大于 0。")
        if self.apk_max_consecutive_failures < 1:
            raise ConfigError("APK 连续失败熔断阈值必须大于 0。")
        if self.risk_poll_initial_wait < 0:
            raise ConfigError("风控轮询首次等待不能为负数。")
        if self.risk_poll_backoff_factor < 1:
            raise ConfigError("风控轮询退避倍数必须 >= 1。")
        if self.risk_poll_max_wait < 0 or self.risk_poll_max_duration < 0:
            raise ConfigError("风控轮询等待上限不能为负数。")
