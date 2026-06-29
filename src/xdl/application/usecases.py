# -*- coding: utf-8 -*-
"""用例（应用层，见 docs/architecture.md §4）。

MVP：解析单曲并下载；专辑顺序批量下载（文件级跳过 + 逐集容错 + 结尾汇总）。
任务引擎（持久化/并发/字节级续传/增量游标）留待后续阶段。
用例只依赖端口与领域，不感知具体适配器。
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field

from ..domain import (Track, Album, AlbumTrack, Quality, NamingPolicy,
                      parse_track_id, parse_album_id)
from ..errors import XdlError, AuthError, ApiError
from ..ports import Source, MediaSink, ProgressReporter

_EXTS = (".m4a", ".mp3")


def _note(reporter: ProgressReporter | None, msg: str) -> None:
    if reporter is not None:
        reporter.note(msg)


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


@dataclass
class AlbumResult:
    """专辑下载汇总。"""
    album_title: str
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)        # 已存在而跳过
    failed: list[tuple[AlbumTrack, str]] = field(default_factory=list)
    incomplete: bool = False                                 # 清单未取全（如未登录）

    def summary(self) -> str:
        line = (f"专辑《{self.album_title}》：下载 {len(self.downloaded)}，"
                f"跳过 {len(self.skipped)}，失败 {len(self.failed)}。")
        if self.incomplete:
            line += "（注意：曲目清单未取全，登录后重跑可补齐）"
        return line


class DownloadAlbumUseCase:
    """专辑顺序批量下载：解析清单 → 按区间逐集解析 playUrl 并落盘。

    复用 source 的批量会话（由 Facade 负责 open/close），逐集失败不中断整轮，
    已存在的目标文件直接跳过（文件级断点续传）。
    """

    def __init__(self, source: Source, sink: MediaSink, download_dir: str,
                 request_interval: tuple[float, float] = (1.0, 3.0)):
        self._source = source
        self._sink = sink
        self._download_dir = download_dir
        self._interval = request_interval

    def execute(self, target: str, quality: Quality,
                start: int | None = None, end: int | None = None,
                reporter: ProgressReporter | None = None) -> AlbumResult:
        album: Album = self._source.get_album(parse_album_id(target))
        selected = album.select_range(start, end)

        result = AlbumResult(album.title, incomplete=not album.is_complete)
        if not album.is_complete:
            _note(reporter, f"清单仅取到 {len(album.tracks)}/{album.total} 集"
                            "（未登录或受限）。")
        if not selected:
            _note(reporter, "选定区间内无曲目。")
            return result

        album_dir = os.path.join(self._download_dir, NamingPolicy.sanitize(album.title))
        width = len(str(album.total or len(album.tracks) or 1))

        did_resolve = False
        for at in selected:
            label = f"[{at.index}/{album.total or len(album.tracks)}] {at.title}"
            existing = self._existing_path(album_dir, at, width)
            if existing:
                _note(reporter, f"{label} — 已存在，跳过")
                result.skipped.append(existing)
                continue

            if did_resolve:                 # 克制：逐集解析间随机停顿，避免触发频率风控
                time.sleep(random.uniform(*self._interval))
            did_resolve = True

            _note(reporter, label)
            try:
                path = self._download_one(at, quality, album_dir, width, reporter)
                result.downloaded.append(path)
            except XdlError as e:
                _note(reporter, f"  失败：{e}")
                result.failed.append((at, str(e)))

        return result

    # ---- 内部 ----
    def _download_one(self, at: AlbumTrack, quality: Quality, album_dir: str,
                      width: int, reporter: ProgressReporter | None) -> str:
        track = self._source.get_track(at.track_id)
        play = track.select(quality)
        if not play or not play.url:
            if track.is_paid and not track.is_authorized:
                raise AuthError("付费内容且当前账号无权播放。")
            raise ApiError("未找到可用的播放地址。")
        filename = NamingPolicy.track_filename(at.title, play.ext,
                                               index=at.index, index_width=width)
        target_path = os.path.join(album_dir, filename)
        self._sink.write(play.url, target_path, reporter)
        return target_path

    def _existing_path(self, album_dir: str, at: AlbumTrack, width: int) -> str | None:
        """音质未知前，按「序号+标题」词干匹配任一已知扩展名的成品文件。"""
        stem = NamingPolicy.track_filename(at.title, "", index=at.index,
                                           index_width=width)
        for ext in _EXTS:
            cand = os.path.join(album_dir, stem + ext)
            if os.path.exists(cand):
                return cand
        return None
