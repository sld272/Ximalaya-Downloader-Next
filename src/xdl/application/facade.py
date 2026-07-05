# -*- coding: utf-8 -*-
"""门面（库的公开 API，见 docs/architecture.md §11）。

各前端（CLI / 未来 GUI/Web）只依赖这一层。公开方法保持**同步**签名，内部用
asyncio 驱动异步解析/并发（`asyncio.run` 收口），前端零改动。
"""
from __future__ import annotations

import asyncio

from ..domain import Quality, parse_range
from ..settings import Settings
from .usecases import (DownloadTrackUseCase, DownloadAlbumUseCase, AlbumResult,
                       ResumeUseCase, RetryPolicy)


class Facade:
    def __init__(self, source, sink, settings: Settings, store=None):
        self._source = source
        self._sink = sink
        self._settings = settings
        self._store = store

    @classmethod
    def from_config(cls, settings: Settings | None = None) -> "Facade":
        from ..composition import build_facade
        return build_facade(settings)

    def _retry_policy(self) -> RetryPolicy:
        s = self._settings
        return RetryPolicy(max_attempts=s.max_attempts,
                           backoff_base=s.retry_backoff_base,
                           cooldown=s.cooldown,
                           global_rounds=s.global_retry_rounds)

    def login(self) -> str:
        """打开浏览器登录并保存会话，返回保存路径。"""
        return self._source.interactive_login()

    def download_track(self, target: str, quality: str | None = None,
                       reporter=None) -> str:
        """下载单个音频，返回落盘路径。"""
        return asyncio.run(self._download_track(target, quality, reporter))

    def download_album(self, target: str, quality: str | None = None,
                       range_: str | None = None, reporter=None) -> AlbumResult:
        """并发批量下载专辑，返回下载汇总。range_ 形如 '1-20' / '5-' / '-10' / '7'。"""
        return asyncio.run(self._download_album(target, quality, range_, reporter))

    def resume(self, reporter=None) -> list[AlbumResult]:
        """继续任务库中未完成的专辑下载。"""
        return asyncio.run(self._resume(reporter))

    # ---- 内部异步实现 ----
    async def _download_track(self, target, quality, reporter) -> str:
        q = Quality(quality or self._settings.default_quality)
        usecase = DownloadTrackUseCase(self._source, self._sink,
                                       self._settings.download_dir,
                                       retry=self._retry_policy())
        await self._source.open()
        try:
            return await usecase.execute(target, q, reporter)
        finally:
            await self._source.close()

    async def _download_album(self, target, quality, range_, reporter) -> AlbumResult:
        q = Quality(quality or self._settings.default_quality)
        start, end = parse_range(range_)
        usecase = DownloadAlbumUseCase(self._source, self._sink,
                                       self._settings.download_dir,
                                       concurrency=self._settings.max_concurrency,
                                       retry=self._retry_policy(),
                                       store=self._store)
        # 全程复用一个 Chrome 会话（共享上下文里按需开 page 并发解析）
        await self._source.open()
        try:
            return await usecase.execute(target, q, start, end, reporter)
        finally:
            await self._source.close()

    async def _resume(self, reporter) -> list[AlbumResult]:
        if self._store is None:
            return []
        usecase = ResumeUseCase(self._source, self._sink,
                                self._settings.download_dir, self._store,
                                concurrency=self._settings.max_concurrency,
                                retry=self._retry_policy())
        return await usecase.execute(reporter)
