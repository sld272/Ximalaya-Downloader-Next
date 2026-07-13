# -*- coding: utf-8 -*-
"""装配根（依赖注入，见 docs/architecture.md §1、§3）。

按配置把适配器接到端口上，组装出供前端调用的门面。
未来要替换实现（如新增本地签名、换音源），只改这里。
"""
from __future__ import annotations

from .settings import Settings
from .adapters import (Www2Decoder, FileSink, ChromeSource, HttpSource,
                       PySignProvider, SqliteTaskStore)
from .application import Facade
from .risk import RiskEventRecorder


def build_facade(settings: Settings | None = None) -> Facade:
    settings = settings or Settings()
    decoder = Www2Decoder()
    risk_recorder = RiskEventRecorder(settings.risk_log_path)

    source = _build_source(settings, decoder, risk_recorder)
    sink = FileSink(http_timeout=settings.http_timeout)

    def store_factory():
        return SqliteTaskStore(settings.task_db_path)

    return Facade(source, sink, settings, store_factory=store_factory)


def _build_source(settings: Settings, decoder, risk_recorder):
    """按 `settings.source_backend` 装配在线音源实现。"""
    backend = (settings.source_backend or "").strip().lower()
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
        )
    # 默认：CDP 接管真实 Chrome（已被实测确认会触发 CDP inspector 痕迹风控，作为
    # 兜底保留；想绕开请改 `source_backend = "http"`）。
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
    )
