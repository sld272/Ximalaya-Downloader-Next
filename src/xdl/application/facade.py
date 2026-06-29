# -*- coding: utf-8 -*-
"""门面（库的公开 API，见 docs/architecture.md §11）。

各前端（CLI / 未来 GUI/Web）只依赖这一层。MVP 提供 login 与 download_track，
后续在此扩展 download_album / sync_album / search 等，前端零改动复用。
"""
from __future__ import annotations

from ..domain import Quality, parse_range
from ..settings import Settings
from .usecases import DownloadTrackUseCase, DownloadAlbumUseCase, AlbumResult


class Facade:
    def __init__(self, source, sink, settings: Settings):
        self._source = source
        self._sink = sink
        self._settings = settings

    @classmethod
    def from_config(cls, settings: Settings | None = None) -> "Facade":
        from ..composition import build_facade
        return build_facade(settings)

    def login(self) -> str:
        """打开浏览器登录并保存会话，返回保存路径。"""
        return self._source.interactive_login()

    def download_track(self, target: str, quality: str | None = None,
                       reporter=None) -> str:
        """下载单个音频，返回落盘路径。"""
        q = Quality(quality or self._settings.default_quality)
        usecase = DownloadTrackUseCase(self._source, self._sink,
                                       self._settings.download_dir)
        return usecase.execute(target, q, reporter)

    def download_album(self, target: str, quality: str | None = None,
                       range_: str | None = None, reporter=None) -> AlbumResult:
        """顺序批量下载专辑，返回下载汇总。range_ 形如 '1-20' / '5-' / '-10' / '7'。"""
        q = Quality(quality or self._settings.default_quality)
        start, end = parse_range(range_)
        usecase = DownloadAlbumUseCase(self._source, self._sink,
                                       self._settings.download_dir)
        # 全程复用一个浏览器会话（逐集导航复用，避免每集冷启动）
        self._source.open()
        try:
            return usecase.execute(target, q, start, end, reporter)
        finally:
            self._source.close()
