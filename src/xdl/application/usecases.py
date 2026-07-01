# -*- coding: utf-8 -*-
"""用例（应用层，见 docs/architecture.md §4、§8.3）。

单曲解析下载；专辑批量下载（**有界并发** + 文件级跳过 + 错误分级退避重试 +
失败收尾轮 + 结尾汇总）。持久化/字节级续传/增量游标留待后续阶段。
用例只依赖端口与领域，不感知具体适配器。解析走 async（可并发），下载放线程池。
"""
from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

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
            base = self.cooldown
        else:
            base = self.backoff_base * (2 ** (attempt - 1))
        return base + random.uniform(0, base * 0.3)


async def _run_with_retry(fn: Callable[[], Awaitable[_T]], policy: RetryPolicy,
                          reporter: ProgressReporter | None, label: str = "") -> _T:
    """按策略执行 async fn；仅对 retryable 异常重试，否则立即抛出。"""
    last: XdlError | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await fn()
        except XdlError as e:
            last = e
            if not e.retryable or attempt >= policy.max_attempts:
                raise
            wait = policy.wait_for(e, attempt)
            _note(reporter, f"  {label}第 {attempt} 次失败（{e.category}），"
                            f"{wait:.0f}s 后重试…")
            await asyncio.sleep(wait)
    raise last  # pragma: no cover


class DownloadTrackUseCase:
    def __init__(self, source: Source, sink: MediaSink, download_dir: str,
                 retry: RetryPolicy | None = None):
        self._source = source
        self._sink = sink
        self._download_dir = download_dir
        self._retry = retry or RetryPolicy()

    async def execute(self, target: str, quality: Quality,
                      reporter: ProgressReporter | None = None) -> str:
        track_id = parse_track_id(target)

        async def _do() -> str:
            track: Track = await self._source.get_track(track_id)
            play = track.select(quality)
            if not play or not play.url:
                if track.is_paid and not track.is_authorized:
                    raise AuthError(f"《{track.title}》为付费内容且当前账号无权播放。")
                raise ApiError(f"未找到可用的播放地址（曲目：{track.title}）。")
            filename = NamingPolicy.track_filename(track.title, play.ext)
            target_path = os.path.join(self._download_dir, filename)
            await asyncio.to_thread(self._sink.write, play.url, target_path, reporter)
            return target_path

        return await _run_with_retry(_do, self._retry, reporter)


@dataclass
class AlbumResult:
    """专辑下载汇总。"""
    album_title: str
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[AlbumTrack, str]] = field(default_factory=list)
    incomplete: bool = False

    def summary(self) -> str:
        line = (f"专辑《{self.album_title}》：下载 {len(self.downloaded)}，"
                f"跳过 {len(self.skipped)}，失败 {len(self.failed)}。")
        if self.incomplete:
            line += "（注意：曲目清单未取全，登录后重跑可补齐）"
        return line


class DownloadAlbumUseCase:
    """专辑有界并发批量下载：解析清单 → 并发解析 playUrl 并落盘 → 失败收尾轮。

    并发由信号量限定（默认见 Settings.max_concurrency）；逐集失败不中断整轮；
    已存在文件直接跳过（文件级续传）；解析失败按错误类型退避重试。
    """

    def __init__(self, source: Source, sink: MediaSink, download_dir: str,
                 concurrency: int = 4, retry: RetryPolicy | None = None):
        self._source = source
        self._sink = sink
        self._download_dir = download_dir
        self._concurrency = max(1, concurrency)
        self._retry = retry or RetryPolicy()

    async def execute(self, target: str, quality: Quality,
                      start: int | None = None, end: int | None = None,
                      reporter: ProgressReporter | None = None) -> AlbumResult:
        album: Album = await self._source.get_album(parse_album_id(target))
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

        # 预筛已存在（同步、快，不占并发）
        todo: list[AlbumTrack] = []
        for at in selected:
            existing = self._existing_path(album_dir, at, width)
            if existing:
                _note(reporter, f"[{at.index}/{total}] {at.title} — 已存在，跳过")
                result.skipped.append(existing)
            else:
                todo.append(at)

        _note(reporter, f"开始并发下载 {len(todo)} 集（并发 {self._concurrency}）")
        failures = await self._run_batch(todo, quality, album_dir, width, total,
                                         reporter, result)

        # 失败收尾轮：跨轮间隔后统一重试「可重试」的残余失败项
        for rnd in range(1, self._retry.global_rounds + 1):
            retryable = [(at, e) for at, e in failures if e.retryable]
            if not retryable:
                break
            _note(reporter, f"== 失败收尾第 {rnd}/{self._retry.global_rounds} 轮："
                            f"重试 {len(retryable)} 项 ==")
            if self._retry.cooldown:
                await asyncio.sleep(self._retry.cooldown)
            still = [(at, e) for at, e in failures if not e.retryable]
            more = await self._run_batch([at for at, _ in retryable], quality,
                                         album_dir, width, total, reporter, result)
            failures = still + more

        result.failed = [(at, str(e)) for at, e in failures]
        return result

    # ---- 内部 ----
    async def _run_batch(self, tracks, quality, album_dir, width, total,
                         reporter, result) -> list[tuple[AlbumTrack, XdlError]]:
        sem = asyncio.Semaphore(self._concurrency)
        failures: list[tuple[AlbumTrack, XdlError]] = []

        async def worker(at: AlbumTrack) -> None:
            async with sem:
                await asyncio.sleep(random.uniform(0, 0.3))   # 轻微错峰
                label = f"[{at.index}/{total}] {at.title}"
                try:
                    path = await self._resolve(at, quality, album_dir, width,
                                               reporter, label)
                    result.downloaded.append(path)
                    _note(reporter, f"  ✓ {label}")
                except XdlError as e:
                    _note(reporter, f"  ✗ {label} — {e}")
                    failures.append((at, e))

        await asyncio.gather(*(worker(at) for at in tracks))
        return failures

    async def _resolve(self, at, quality, album_dir, width, reporter, label) -> str:
        return await _run_with_retry(
            lambda: self._download_one(at, quality, album_dir, width),
            self._retry, reporter, label=f"{label} ")

    async def _download_one(self, at, quality, album_dir, width) -> str:
        track = await self._source.get_track(at.track_id)
        play = track.select(quality)
        if not play or not play.url:
            if track.is_paid and not track.is_authorized:
                raise AuthError("付费内容且当前账号无权播放。")
            raise ApiError("未找到可用的播放地址。")
        filename = NamingPolicy.track_filename(at.title, play.ext,
                                               index=at.index, index_width=width)
        target_path = os.path.join(album_dir, filename)
        # 下载放线程池：多集下载并行、且不挡住事件循环里的解析
        await asyncio.to_thread(self._sink.write, play.url, target_path, None)
        return target_path

    def _existing_path(self, album_dir: str, at: AlbumTrack, width: int) -> str | None:
        stem = NamingPolicy.track_filename(at.title, "", index=at.index,
                                           index_width=width)
        for ext in _EXTS:
            cand = os.path.join(album_dir, stem + ext)
            if os.path.exists(cand):
                return cand
        return None
