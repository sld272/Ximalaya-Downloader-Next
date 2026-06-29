# -*- coding: utf-8 -*-
"""用例（应用层，见 docs/architecture.md §4、§8.3）。

MVP：解析单曲并下载；专辑顺序批量下载（文件级跳过 + 逐集容错 + 结尾汇总）。
本阶段补上「错误分级退避重试 + 失败收尾轮」（任务引擎的第一块切片）；
持久化/并发/字节级续传/增量游标留待后续阶段。
用例只依赖端口与领域，不感知具体适配器。
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from ..domain import (Track, Album, AlbumTrack, Quality, NamingPolicy,
                      parse_track_id, parse_album_id)
from ..errors import XdlError, AuthError, ApiError
from ..ports import Source, MediaSink, ProgressReporter

_EXTS = (".m4a", ".mp3")
_T = TypeVar("_T")


def _note(reporter: ProgressReporter | None, msg: str) -> None:
    if reporter is not None:
        reporter.note(msg)


@dataclass
class RetryPolicy:
    """重试策略（见架构 §8.3）。错误类型决定是否重试与等待时长。"""
    max_attempts: int = 3          # 单任务即时重试上限
    backoff_base: float = 1.5      # 网络/签名类退避基数（秒，按尝试次指数增长）
    cooldown: float = 30.0         # 限流(ret=1001)类冷却（秒）
    global_rounds: int = 2         # 失败收尾轮数

    def wait_for(self, err: XdlError, attempt: int) -> float:
        if isinstance(err, ApiError) and getattr(err, "ret", None) == 1001:
            base = self.cooldown          # 频率风控：冷却久一点让其复位
        else:
            base = self.backoff_base * (2 ** (attempt - 1))   # 指数退避
        return base + random.uniform(0, base * 0.3)           # 抖动


def _run_with_retry(fn: Callable[[], _T], policy: RetryPolicy,
                    reporter: ProgressReporter | None, label: str = "") -> _T:
    """按策略执行 fn；仅对 retryable 异常重试，否则立即抛出。"""
    last: XdlError | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except XdlError as e:
            last = e
            if not e.retryable or attempt >= policy.max_attempts:
                raise
            wait = policy.wait_for(e, attempt)
            _note(reporter, f"  {label}第 {attempt} 次失败（{e.category}），"
                            f"{wait:.0f}s 后重试…")
            time.sleep(wait)
    raise last  # pragma: no cover  （循环必然 return 或 raise）


class DownloadTrackUseCase:
    def __init__(self, source: Source, sink: MediaSink, download_dir: str,
                 retry: RetryPolicy | None = None):
        self._source = source
        self._sink = sink
        self._download_dir = download_dir
        self._retry = retry or RetryPolicy()

    def execute(self, target: str, quality: Quality,
                reporter: ProgressReporter | None = None) -> str:
        track_id = parse_track_id(target)

        def _do() -> str:
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

        return _run_with_retry(_do, self._retry, reporter)


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
                 request_interval: tuple[float, float] = (1.0, 3.0),
                 retry: RetryPolicy | None = None):
        self._source = source
        self._sink = sink
        self._download_dir = download_dir
        self._interval = request_interval
        self._retry = retry or RetryPolicy()

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
        total = album.total or len(album.tracks)

        # 主轮：逐集解析下载（含单集即时退避重试）；失败暂存待收尾
        failures: list[tuple[AlbumTrack, XdlError]] = []
        did_resolve = False
        for at in selected:
            label = f"[{at.index}/{total}] {at.title}"
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
                result.downloaded.append(
                    self._resolve(at, quality, album_dir, width, reporter, label))
            except XdlError as e:
                _note(reporter, f"  失败：{e}")
                failures.append((at, e))

        # 失败收尾轮：跨整轮的时间间隔后，统一重试「可重试」的残余失败项
        for rnd in range(1, self._retry.global_rounds + 1):
            retryable = [(at, e) for at, e in failures if e.retryable]
            if not retryable:
                break
            _note(reporter, f"== 失败收尾第 {rnd}/{self._retry.global_rounds} 轮："
                            f"重试 {len(retryable)} 项 ==")
            if self._retry.cooldown:
                time.sleep(self._retry.cooldown)
            still = [(at, e) for at, e in failures if not e.retryable]
            for at, _ in retryable:
                time.sleep(random.uniform(*self._interval))
                label = f"[{at.index}/{total}] {at.title}"
                _note(reporter, f"重试 {label}")
                try:
                    result.downloaded.append(
                        self._resolve(at, quality, album_dir, width, reporter, label))
                except XdlError as e:
                    _note(reporter, f"  仍失败：{e}")
                    still.append((at, e))
            failures = still

        result.failed = [(at, str(e)) for at, e in failures]
        return result

    # ---- 内部 ----
    def _resolve(self, at: AlbumTrack, quality: Quality, album_dir: str,
                 width: int, reporter: ProgressReporter | None, label: str) -> str:
        return _run_with_retry(
            lambda: self._download_one(at, quality, album_dir, width, reporter),
            self._retry, reporter, label=f"{label} ")

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
