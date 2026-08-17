"""APK 专属媒体 sink：每集 URL 短生命周期、失效后最多刷新一次。"""
from __future__ import annotations

import os

import requests

from .sink_file import FileSink
from ..errors import NetworkError


class ApkMediaSink(FileSink):
    def __init__(self, source, http_timeout: int = 60):
        super().__init__(http_timeout=http_timeout)
        self._source = source

    def write_track(self, url, track_id, quality, target_path, reporter,
                    cancel=None, progress_sink=None, expected_total=0):
        os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
        part_path = target_path + ".part"
        current_url = url
        refreshed = False
        while True:
            try:
                self._write_stream(
                    current_url, target_path, part_path,
                    {"User-Agent": "ting_9.5.1(Android)", "requestType": "download"},
                    reporter, cancel, progress_sink, expected_total,
                )
                return
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status not in {401, 403, 404} or refreshed:
                    raise NetworkError(f"APK 媒体下载失败: HTTP {status}") from exc
                refreshed = True
                track = self._source.resolve_track_sync(track_id, quality)
                play = track.select(quality)
                if not play or not play.url:
                    raise NetworkError("APK 媒体连接刷新后仍无可用地址。")
                current_url = play.url
            except requests.RequestException as exc:
                raise NetworkError(f"APK 媒体下载失败: {exc}") from exc
