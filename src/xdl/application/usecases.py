# -*- coding: utf-8 -*-
"""用例（应用层，见 docs/architecture.md §4）。

MVP：解析单曲并下载。专辑同步、搜索、本地解码等留待后续阶段。
用例只依赖端口与领域，不感知具体适配器。
"""
from __future__ import annotations

import os

from ..domain import Track, Quality, NamingPolicy, parse_track_id
from ..errors import AuthError, ApiError
from ..ports import Source, MediaSink, ProgressReporter


class DownloadTrackUseCase:
    def __init__(self, source: Source, sink: MediaSink, download_dir: str):
        self._source = source
        self._sink = sink
        self._download_dir = download_dir

    def execute(self, target: str, quality: Quality,
                reporter: ProgressReporter | None = None) -> str:
        track_id = parse_track_id(target)
        track: Track = self._source.get_track(track_id)

        play = track.select(quality)
        if not play or not play.url:
            if track.is_paid and not track.is_authorized:
                raise AuthError(f"《{track.title}》为付费内容且当前账号无权播放。")
            raise ApiError(f"未找到可用的播放地址（曲目：{track.title}）。")

        filename = NamingPolicy.track_filename(track.title, play.ext)
        target_path = os.path.join(self._download_dir, filename)
        self._sink.write(play.url, target_path, reporter)
        return target_path
