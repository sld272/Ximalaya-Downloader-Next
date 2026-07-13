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
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

from ..domain import (Track, Album, AlbumTrack, DownloadTask, Quality, NamingPolicy,
                      parse_track_id, parse_album_id)
from ..errors import (XdlError, AuthError, ApiError, CancelledByUser,
                      RiskControlError)
from ..ports import Source, MediaSink, ProgressReporter, TaskStore

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
        if isinstance(err, RiskControlError):
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
        except CancelledByUser:
            raise
        except XdlError as e:
            last = e
            # 风控的 retryable 仅表示可以在未来的人工 resume 中恢复；当前运行
            # 必须立即停止，不能把即时重试变成持续冲击。
            if isinstance(e, RiskControlError):
                raise
            if not e.retryable or attempt >= policy.max_attempts:
                raise
            wait = policy.wait_for(e, attempt)
            _note(reporter, f"  {label}第 {attempt} 次失败（{e.category}），"
                            f"{wait:.0f}s 后重试…")
            await asyncio.sleep(wait)
    raise last  # pragma: no cover


class DownloadTrackUseCase:
    def __init__(self, source: Source, sink: MediaSink, download_dir: str,
                 retry: RetryPolicy | None = None,
                 store: TaskStore | None = None,
                 cancel_event: threading.Event | None = None):
        self._source = source
        self._sink = sink
        self._download_dir = download_dir
        self._retry = retry or RetryPolicy()
        self._store = store
        self._cancel_event = cancel_event

    async def execute(self, target: str, quality: Quality,
                      reporter: ProgressReporter | None = None) -> str:
        track_id = parse_track_id(target)
        holder: dict[str, DownloadTask | None] = {"task": None}

        async def _do() -> str:
            track: Track = await self._source.get_track(track_id)
            play = track.select(quality)
            if not play or not play.url:
                if track.is_paid and not track.is_authorized:
                    raise AuthError(f"《{track.title}》为付费内容且当前账号无权播放。")
                raise ApiError(f"未找到可用的播放地址（曲目：{track.title}）。")
            filename = NamingPolicy.track_filename(track.title, play.ext)
            target_path = os.path.join(self._download_dir, filename)
            # 单曲也纳入任务表：前端面板可见、进度/续传持久化（album_id 留空）。
            task = await self._prepare_task(track_id, quality.value, track.title,
                                            target_path, reporter)
            holder["task"] = task
            if task is not None and task.id is not None:
                await self._store_call(reporter, self._store.mark_downloading, task.id)
            await asyncio.to_thread(self._sink.write, play.url, target_path, reporter,
                                    self._cancel_event, self._progress_sink(task), 0)
            if task is not None and task.id is not None:
                await self._store_call(reporter, self._store.mark_done,
                                       task.id, target_path)
            return target_path

        try:
            return await _run_with_retry(_do, self._retry, reporter)
        except CancelledByUser:
            await self._requeue(holder["task"], reporter)
            raise
        except XdlError as e:
            await self._fail(holder["task"], e, reporter)
            raise

    # ---- 任务库（与专辑逐集流程一致的最小实现） ----
    async def _prepare_task(self, track_id, quality, title, target_path, reporter):
        if self._store is None:
            return None
        task = DownloadTask(track_id=track_id, album_id="", title=title,
                            quality=quality, album_index=0, target_path=target_path)
        rows = await self._store_call(reporter, self._store.upsert_pending, [task],
                                      default=None)
        return rows[0] if rows else None

    async def _requeue(self, task, reporter):
        if task is not None and task.id is not None:
            await self._store_call(reporter, self._store.upsert_pending, [task])

    async def _fail(self, task, e, reporter):
        if task is not None and task.id is not None:
            await self._store_call(reporter, self._store.mark_failed,
                                   task.id, e.category, str(e), e.retryable)

    async def _store_call(self, reporter, fn, *args, default=None):
        if self._store is None:
            return default
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as e:
            _note(reporter, f"任务库操作失败，已继续下载：{e}")
            return default

    def _progress_sink(self, task: DownloadTask | None):
        if self._store is None or task is None or task.id is None:
            return None

        def persist(done: int, total: int) -> None:
            try:
                self._store.record_progress(task.id, done, total)
            except Exception:
                pass

        return persist


@dataclass
class AlbumResult:
    """专辑下载汇总。"""
    album_title: str
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[AlbumTrack, str]] = field(default_factory=list)
    incomplete: bool = False
    stopped: bool = False

    def summary(self) -> str:
        line = (f"专辑《{self.album_title}》：下载 {len(self.downloaded)}，"
                f"跳过 {len(self.skipped)}，失败 {len(self.failed)}。")
        if self.incomplete:
            line += "（注意：曲目清单未取全，登录后重跑可补齐）"
        return line


@dataclass
class _AlbumWorkItem:
    track: AlbumTrack
    task: DownloadTask | None = None


class DownloadAlbumUseCase:
    """专辑有界并发批量下载：解析清单 → 并发解析 playUrl 并落盘 → 失败收尾轮。

    并发由信号量限定（默认见 Settings.max_concurrency）；逐集失败不中断整轮；
    已存在文件直接跳过（文件级续传）；解析失败按错误类型退避重试。
    """

    def __init__(self, source: Source, sink: MediaSink, download_dir: str,
                 concurrency: int = 1, retry: RetryPolicy | None = None,
                 store: TaskStore | None = None,
                 stop_event: asyncio.Event | None = None,
                 cancel_event: threading.Event | None = None):
        self._source = source
        self._sink = sink
        self._download_dir = download_dir
        self._concurrency = max(1, concurrency)
        self._retry = retry or RetryPolicy()
        self._store = store
        self._stop_event = stop_event
        self._cancel_event = cancel_event

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

        task_rows = await self._prepare_tasks(album, selected, quality, width,
                                              reporter)
        task_by_track = ({t.track_id: t for t in task_rows}
                         if task_rows is not None else None)

        # 预筛已存在（同步、快，不占并发）；文件系统是完成态的最终真相。
        work: list[_AlbumWorkItem] = []
        for at in selected:
            existing = self._existing_path(album_dir, at, width)
            task = task_by_track.get(at.track_id) if task_by_track is not None else None
            if existing:
                _note(reporter, f"[{at.index}/{total}] {at.title} — 已存在，跳过")
                result.skipped.append(existing)
                if task and task.id is not None:
                    await self._store_call(reporter, self._store.mark_done,
                                           task.id, existing)
            else:
                work.append(_AlbumWorkItem(at, task))

        failures = await self._run_work_items(work, quality, album_dir, width,
                                              total, reporter, result)
        result.failed = [(item.track, str(e)) for item, e in failures]
        return result

    async def resume_tasks(self, album_id: str, album_title: str,
                           tasks: list[DownloadTask], quality: Quality,
                           total_known: int = 0,
                           reporter: ProgressReporter | None = None) -> AlbumResult:
        result = AlbumResult(album_title)
        if not tasks:
            return result
        album_dir = os.path.join(self._download_dir, NamingPolicy.sanitize(album_title))
        total = total_known or max((t.album_index for t in tasks), default=len(tasks))
        width = self._resume_index_width(tasks, total)

        work: list[_AlbumWorkItem] = []
        for task in tasks:
            at = AlbumTrack(track_id=task.track_id, title=task.title,
                            index=task.album_index)
            existing = self._existing_path(album_dir, at, width)
            if existing:
                _note(reporter, f"[{at.index}/{total}] {at.title} — 已存在，跳过")
                result.skipped.append(existing)
                if task.id is not None:
                    await self._store_call(reporter, self._store.mark_done,
                                           task.id, existing)
            else:
                work.append(_AlbumWorkItem(at, task))

        failures = await self._run_work_items(work, quality, album_dir, width,
                                              total, reporter, result)
        result.failed = [(item.track, str(e)) for item, e in failures]
        return result

    # ---- 内部 ----
    async def _prepare_tasks(self, album: Album, selected: list[AlbumTrack],
                             quality: Quality, width: int, reporter
                             ) -> list[DownloadTask] | None:
        if self._store is None:
            return None
        tasks = [
            DownloadTask(
                track_id=at.track_id,
                album_id=album.album_id,
                title=at.title,
                quality=quality.value,
                album_index=at.index,
                index_width=width,
            )
            for at in selected
        ]
        await self._store_call(reporter, self._store.save_album_meta,
                               album.album_id, album.title,
                               album.total or len(album.tracks))
        return await self._store_call(reporter, self._store.upsert_pending,
                                      tasks, default=None)

    async def _run_work_items(self, work: list[_AlbumWorkItem], quality: Quality,
                              album_dir: str, width: int, total: int,
                              reporter, result) -> list[tuple[_AlbumWorkItem, XdlError]]:
        _note(reporter, f"开始并发下载 {len(work)} 集（并发 {self._concurrency}）")
        failures = await self._run_batch(work, quality, album_dir, width, total,
                                         reporter, result)

        # 风控不是普通的逐项失败：首个信号出现后整批熔断，禁止失败收尾轮
        # 自动重新冲击受保护接口。任务保持 retryable，可在冷却/人工确认后 resume。
        if any(isinstance(error, RiskControlError) for _item, error in failures):
            _note(reporter, "检测到平台风控，已熔断本批次；不会自动继续重试。")
            return failures

        # 失败收尾轮：跨轮间隔后统一重试「可重试」的残余失败项。
        for rnd in range(1, self._retry.global_rounds + 1):
            if self._is_stopping():
                break
            retryable = [(item, e) for item, e in failures if e.retryable]
            if not retryable:
                break
            _note(reporter, f"== 失败收尾第 {rnd}/{self._retry.global_rounds} 轮："
                            f"重试 {len(retryable)} 项 ==")
            if self._retry.cooldown:
                await asyncio.sleep(self._retry.cooldown)
            await self._requeue_items([item for item, _ in retryable], reporter)
            still = [(item, e) for item, e in failures if not e.retryable]
            more = await self._run_batch([item for item, _ in retryable], quality,
                                         album_dir, width, total, reporter, result)
            failures = still + more
        return failures

    async def _run_batch(self, work: list[_AlbumWorkItem], quality, album_dir,
                         width, total, reporter, result
                         ) -> list[tuple[_AlbumWorkItem, XdlError]]:
        sem = asyncio.Semaphore(self._concurrency)
        failures: list[tuple[_AlbumWorkItem, XdlError]] = []
        risk_error: list[RiskControlError] = []

        async def worker(item: _AlbumWorkItem) -> None:
            async with sem:
                at = item.track
                if risk_error:
                    failures.append((item, RiskControlError(
                        f"风控熔断：未继续请求（起因：{risk_error[0]}）",
                        ret=risk_error[0].ret,
                    )))
                    return
                if self._is_stopping():
                    return
                await asyncio.sleep(random.uniform(0, 0.3))   # 轻微错峰
                if self._is_stopping():
                    return
                label = f"[{at.index}/{total}] {at.title}"
                try:
                    if item.task and item.task.id is not None:
                        await self._store_call(reporter, self._store.mark_downloading,
                                               item.task.id)
                    path = await self._resolve(item, quality, album_dir, width,
                                               reporter, label)
                    result.downloaded.append(path)
                    if item.task and item.task.id is not None:
                        await self._store_call(reporter, self._store.mark_done,
                                               item.task.id, path)
                    _note(reporter, f"  ✓ {label}")
                except CancelledByUser:
                    await self._requeue_items([item], reporter)
                    _note(reporter, f"  ↷ {label} — 已停止，保留待恢复")
                except XdlError as e:
                    if isinstance(e, RiskControlError) and not risk_error:
                        risk_error.append(e)
                    if item.task and item.task.id is not None:
                        await self._store_call(reporter, self._store.mark_failed,
                                               item.task.id, e.category, str(e),
                                               e.retryable)
                    _note(reporter, f"  ✗ {label} — {e}")
                    failures.append((item, e))

        await asyncio.gather(*(worker(item) for item in work))
        return failures

    async def _requeue_items(self, items: list[_AlbumWorkItem], reporter) -> None:
        if self._store is None:
            return
        tasks = [item.task for item in items if item.task is not None]
        if tasks:
            await self._store_call(reporter, self._store.upsert_pending, tasks)

    async def _store_call(self, reporter, fn, *args, default=None):
        if self._store is None:
            return default
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as e:
            _note(reporter, f"任务库操作失败，已继续下载：{e}")
            return default

    async def _resolve(self, item, quality, album_dir, width, reporter, label) -> str:
        return await _run_with_retry(
            lambda: self._download_one(item.track, quality, album_dir, width,
                                       item.task),
            self._retry, reporter, label=f"{label} ")

    async def _download_one(self, at, quality, album_dir, width,
                            task: DownloadTask | None = None) -> str:
        self._raise_if_stopping()
        track = await self._source.get_track(at.track_id)
        self._raise_if_stopping()
        play = track.select(quality)
        if not play or not play.url:
            if track.is_paid and not track.is_authorized:
                raise AuthError("付费内容且当前账号无权播放。")
            raise ApiError("未找到可用的播放地址。")
        filename = NamingPolicy.track_filename(at.title, play.ext,
                                               index=at.index, index_width=width)
        target_path = os.path.join(album_dir, filename)
        # 下载放线程池：多集下载并行、且不挡住事件循环里的解析
        await asyncio.to_thread(self._sink.write, play.url, target_path, None,
                                self._cancel_event, self._progress_sink(task),
                                task.total_bytes if task else 0)
        return target_path

    def _progress_sink(self, task: DownloadTask | None):
        if self._store is None or task is None or task.id is None:
            return None

        def persist(done: int, total: int) -> None:
            try:
                self._store.record_progress(task.id, done, total)
            except Exception:
                pass

        return persist

    def _is_stopping(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    def _raise_if_stopping(self) -> None:
        if self._is_stopping():
            raise CancelledByUser("用户请求停止下载。")

    def _existing_path(self, album_dir: str, at: AlbumTrack, width: int) -> str | None:
        stem = NamingPolicy.track_filename(at.title, "", index=at.index,
                                           index_width=width)
        for ext in _EXTS:
            cand = os.path.join(album_dir, stem + ext)
            if os.path.exists(cand):
                return cand
        return None

    def _resume_index_width(self, tasks: list[DownloadTask], total: int) -> int:
        stored = max((t.index_width for t in tasks), default=0)
        if stored > 0:
            return stored
        return len(str(total or len(tasks) or 1))


class ResumeUseCase:
    """从任务库恢复未完成的专辑任务。"""

    def __init__(self, source: Source, sink: MediaSink, download_dir: str,
                 store: TaskStore, concurrency: int = 4,
                 retry: RetryPolicy | None = None,
                 stop_event: asyncio.Event | None = None,
                 cancel_event: threading.Event | None = None):
        self._source = source
        self._sink = sink
        self._download_dir = download_dir
        self._store = store
        self._concurrency = max(1, concurrency)
        self._retry = retry or RetryPolicy()
        self._stop_event = stop_event
        self._cancel_event = cancel_event

    async def execute(self, reporter: ProgressReporter | None = None) -> list[AlbumResult]:
        stale = await self._store_call(reporter, self._store.requeue_stale, default=0)
        retryable = await self._store_call(reporter, self._store.requeue_retryable_failed,
                                           default=0)
        if stale or retryable:
            _note(reporter, f"已恢复 {stale + retryable} 个未完成任务。")

        albums = await self._store_call(reporter, self._store.pending_albums,
                                        default=[])
        if not albums:
            _note(reporter, "没有未完成任务。")
            return []

        downloader = DownloadAlbumUseCase(
            self._source, self._sink, self._download_dir,
            concurrency=self._concurrency, retry=self._retry, store=self._store,
            stop_event=self._stop_event, cancel_event=self._cancel_event,
        )
        results: list[AlbumResult] = []
        await self._source.open()
        try:
            for album_id, title, _count in albums:
                tasks = await self._store_call(reporter, self._store.pending_tasks,
                                               album_id, default=[])
                if not tasks:
                    continue
                total = await self._store_call(reporter, self._store.album_total,
                                               album_id, default=0)
                merged = AlbumResult(title)
                for quality_value, group in self._by_quality(tasks).items():
                    try:
                        quality = Quality(quality_value)
                    except ValueError:
                        for task in group:
                            merged.failed.append((
                                AlbumTrack(task.track_id, task.title, task.album_index),
                                f"未知音质: {quality_value}",
                            ))
                            if task.id is not None:
                                await self._store_call(
                                    reporter, self._store.mark_failed,
                                    task.id, "api", f"未知音质: {quality_value}", False,
                                )
                        continue
                    partial = await downloader.resume_tasks(
                        album_id, title, group, quality,
                        total_known=total, reporter=reporter,
                    )
                    merged.downloaded.extend(partial.downloaded)
                    merged.skipped.extend(partial.skipped)
                    merged.failed.extend(partial.failed)
                results.append(merged)
        finally:
            await self._source.close()
        return results

    async def _store_call(self, reporter, fn, *args, default=None):
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as e:
            _note(reporter, f"任务库操作失败，已继续：{e}")
            return default

    def _by_quality(self, tasks: list[DownloadTask]) -> dict[str, list[DownloadTask]]:
        grouped: dict[str, list[DownloadTask]] = defaultdict(list)
        for task in tasks:
            grouped[task.quality].append(task)
        return grouped
