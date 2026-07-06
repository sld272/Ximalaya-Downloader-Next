# -*- coding: utf-8 -*-
"""装配根（依赖注入，见 docs/architecture.md §1、§3）。

按配置把适配器接到端口上，组装出供前端调用的门面。
未来要替换实现（如新增本地签名、换音源），只改这里。
"""
from __future__ import annotations

from .settings import Settings
from .adapters import Www2Decoder, FileSink, ChromeSource, SqliteTaskStore
from .application import Facade


def build_facade(settings: Settings | None = None) -> Facade:
    settings = settings or Settings()

    decoder = Www2Decoder()
    source = ChromeSource(
        decoder,
        chrome_path=settings.chrome_path,
        profile_dir=settings.chrome_profile_dir,
        port=settings.cdp_port,
        resolve_timeout=settings.resolve_timeout,
        headless=settings.chrome_headless,
    )
    sink = FileSink(http_timeout=settings.http_timeout)

    def store_factory():
        return SqliteTaskStore(settings.task_db_path)

    return Facade(source, sink, settings, store_factory=store_factory)
