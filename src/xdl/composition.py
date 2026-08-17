# -*- coding: utf-8 -*-
"""装配根（依赖注入，见 docs/architecture.md §1、§3）。

按配置把适配器接到端口上，组装出供前端调用的门面。
未来要替换实现（如新增本地签名、换音源），只改这里。
"""
from __future__ import annotations

from .settings import Settings
from .adapters import (Www2Decoder, FileSink, ChromeSource, HttpSource,
                       PcHttpSource, PySignProvider, SqliteTaskStore,
                       ApkClient, ApkNativeBridge, ApkSource, ApkStateStore,
                       ApkMediaSink)
from .application import Facade
from .errors import ConfigError
from .risk import RiskEventRecorder


def build_facade(settings: Settings | None = None) -> Facade:
    settings = settings or Settings()
    decoder = Www2Decoder()
    risk_recorder = RiskEventRecorder(settings.risk_log_path)

    source = _build_source(settings, decoder, risk_recorder)
    sink = (ApkMediaSink(source, http_timeout=settings.http_timeout)
            if isinstance(source, ApkSource)
            else FileSink(http_timeout=settings.http_timeout))

    def store_factory():
        return SqliteTaskStore(settings.task_db_path)

    return Facade(source, sink, settings, store_factory=store_factory)


def _build_source(settings: Settings, decoder, risk_recorder):
    """按 `settings.source_backend` 装配在线音源实现。"""
    backend = (settings.source_backend or "http").strip().lower()
    # Settings.__post_init__ 已按 browser 偏好解析出实际浏览器与路径。
    browser = getattr(settings, "resolved_browser", None) or "chrome"
    if backend == "http":
        sign_provider = PySignProvider(
            device_info_path=settings.device_info_path,
        )
        # ChromeSource 仅作 chrome_fallback：用于 `xdl login`（向 Profile 写登录态）
        # 与 `xdl inspect`（列设备标识 key）。这两个命令与"获取播放地址"无关，
        # 在任何后端下都走 ChromeSource；HttpSource 自己只负责纯 HTTP 下载。
        chrome_fallback = ChromeSource(
            decoder,
            chrome_path=settings.chrome_path,
            profile_dir=settings.chrome_profile_dir,
            port=settings.cdp_port,
            resolve_timeout=settings.resolve_timeout,
            headless=settings.chrome_headless,
            risk_recorder=risk_recorder,
            risk_fallback_headful=settings.risk_fallback_headful,
            reset_device_fingerprint=settings.reset_device_fingerprint,
            browser=browser,
        )
        return HttpSource(
            decoder,
            sign_provider,
            chrome_path=settings.chrome_path,
            profile_dir=settings.chrome_profile_dir,
            cookies_cache_path=settings.cookies_cache_path,
            resolve_timeout=settings.resolve_timeout,
            chrome_headless=settings.chrome_headless,
            risk_recorder=risk_recorder,
            chrome_fallback=chrome_fallback,
            impersonate=settings.source_impersonate,
            experiment_rotate_device_on_risk=settings.experiment_rotate_device_on_risk,
            experiment_browser_clear_state=settings.experiment_browser_clear_state,
            experiment_browser_fresh_profile=settings.experiment_browser_fresh_profile,
            experiment_rotate_headless=settings.experiment_rotate_headless,
            experiment_persist_device_info=settings.experiment_persist_device_info,
            experiment_strip_device_cookies=settings.experiment_strip_device_cookies,
            experiment_max_rotations=settings.experiment_max_device_rotations,
            experiment_risk_cooldown_seconds=settings.experiment_risk_cooldown_seconds,
            experiment_require_identity_change=settings.experiment_require_identity_change,
            experiment_rebirth_rounds=settings.experiment_rebirth_rounds,
            device_info_path=settings.device_info_path,
            browser=browser,
        )
    if backend == "pc":
        # PC 桌面端接口：play/v1/show（列表）+ track/quality（播放地址）。
        # 免费曲目走 track/quality 明文地址；VIP/付费曲目 playPathDto 全空，
        # 自动兜底到 baseInfo（PC 客户端抓包链路同款接口，device=win）+
        # WinEcbDecoder 解密加密 playUrlList，因此也需要 SignProvider。
        # 登录仍走 ChromeSource 兜底。
        chrome_fallback = ChromeSource(
            decoder,
            chrome_path=settings.chrome_path,
            profile_dir=settings.chrome_profile_dir,
            port=settings.cdp_port,
            resolve_timeout=settings.resolve_timeout,
            headless=settings.chrome_headless,
            risk_recorder=risk_recorder,
            risk_fallback_headful=settings.risk_fallback_headful,
            reset_device_fingerprint=settings.reset_device_fingerprint,
            browser=browser,
        )
        return PcHttpSource(
            chrome_path=settings.chrome_path,
            profile_dir=settings.chrome_profile_dir,
            cookies_cache_path=settings.cookies_cache_path,
            resolve_timeout=settings.resolve_timeout,
            chrome_headless=settings.chrome_headless,
            risk_recorder=risk_recorder,
            chrome_fallback=chrome_fallback,
            impersonate=settings.source_impersonate,
            browser=browser,
            decoder=decoder,
            sign_provider=PySignProvider(device_info_path=settings.device_info_path),
            device_info_path=settings.device_info_path,
        )
    if backend == "chrome":
        # 兼容路径：CDP 接管真实浏览器（Chrome 或 Edge，跟随 browser 设置）。
        # 实测仍可能触发自动化环境风控，因此不再作为默认下载路径。
        return ChromeSource(
            decoder,
            chrome_path=settings.chrome_path,
            profile_dir=settings.chrome_profile_dir,
            port=settings.cdp_port,
            resolve_timeout=settings.resolve_timeout,
            headless=settings.chrome_headless,
            risk_recorder=risk_recorder,
            risk_fallback_headful=settings.risk_fallback_headful,
            reset_device_fingerprint=settings.reset_device_fingerprint,
            browser=browser,
        )
    if backend == "apk":
        bridge = ApkNativeBridge(
            java_path=settings.apk_java_path,
            signer_jar=settings.apk_signer_jar,
            libcxx=settings.apk_libcxx_path,
            login_so=settings.apk_login_so_path,
            xuid_so=settings.apk_xuid_so_path,
            encrypt_so=settings.apk_encrypt_so_path,
            asset_dir=settings.apk_asset_dir,
            timeout=settings.apk_native_timeout,
        )
        state = ApkStateStore(settings.apk_state_dir)
        return ApkSource(
            ApkClient(bridge, state, timeout=settings.apk_request_timeout),
            max_consecutive_failures=settings.apk_max_consecutive_failures,
        )
    raise ConfigError(
        f"未知音源后端 {settings.source_backend!r}；"
        "可选值为 'http'、'pc'、'chrome' 或 'apk'。"
    )
