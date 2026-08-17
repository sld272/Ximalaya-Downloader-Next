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
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

from ..domain import (Track, Album, AlbumTrack, DownloadTask, Quality, NamingPolicy,
                      parse_track_id, parse_album_id)
from ..errors import (XdlError, AuthError, ConsecutiveFailureError,
                      LoginRequiredError, ApiError, CancelledByUser,
                      RiskControlError)
from ..ports import (Source, QualityAwareSource, MediaSink,
                     TrackResolvingMediaSink, ProgressReporter, TaskStore)

_EXTS = (".m4a", ".mp3")
_T = TypeVar("_T")


def _note(reporter: ProgressReporter | None, msg: str) -> None:
    if reporter is not None:
        reporter.note(msg)


async def _get_track(source: Source, track_id: str, quality: Quality) -> Track:
    """APK 可按音质单次解析；旧 Source 保持原 get_track 调用。"""
    if isinstance(source, QualityAwareSource):
        return await source.get_track_for_quality(track_id, quality)
    return await source.get_track(track_id)


async def _write_media(sink: MediaSink, *, url: str, track_id: str,
                       quality: Quality, target_path: str, reporter,
                       cancel=None, progress_sink=None,
                       expected_total: int = 0) -> None:
    """APK sink 获得刷新上下文；旧 sink 的参数和调用行为不变。"""
    if isinstance(sink, TrackResolvingMediaSink):
        await asyncio.to_thread(
            sink.write_track, url, track_id, quality, target_path, reporter,
            cancel, progress_sink, expected_total,
        )
        return
    await asyncio.to_thread(
        sink.write, url, target_path, reporter, cancel, progress_sink,
        expected_total,
    )


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


@dataclass
class RiskRecoveryPolicy:
    """风控解除轮询策略（默认关闭）。

    开启后，批次因风控熔断且仍有未完成任务时，进入「等待 → 单探针 → 解除后
    继续」循环，直到风控解除、超时或被用户停止。等待期间不发任何请求，每个
    退避周期只发一个探针（对真实待下载曲目调一次受保护接口）。
    """
    enabled: bool = False
    initial_wait: float = 30.0     # 首次探针前等待（秒）
    backoff_factor: float = 2.0    # 每次仍风控的等待倍增
    max_wait: float = 900.0        # 单次等待上限（秒）
    max_duration: float = 3600.0   # 总轮询上限（秒）；0 = 不限

    def wait_for(self, attempt: int) -> float:
        """第 attempt 轮探针前的等待时长（含轻微抖动，避免机械节奏）。"""
        if self.initial_wait <= 0:
            return 0.0
        wait = self.initial_wait
        for _ in range(1, attempt):
            wait *= self.backoff_factor
            if self.max_wait > 0 and wait >= self.max_wait:
                wait = self.max_wait
                break
        return wait + random.uniform(0, max(wait * 0.1, 1.0))


async def _sleep_with_stop(seconds: float, stop_event, cancel_event) -> bool:
    """分片睡眠以便及时响应停止信号；返回 False 表示被停止。"""
    if (stop_event is not None and stop_event.is_set()) or \
            (cancel_event is not None and cancel_event.is_set()):
        return False
    remaining = seconds
    while remaining > 0:
        await asyncio.sleep(min(remaining, 1.0))
        remaining -= 1.0
        if (stop_event is not None and stop_event.is_set()) or \
                (cancel_event is not None and cancel_event.is_set()):
            return False
    return True


async def _await_risk_recovery(source: Source, probe_track_ids: list[str],
                               policy: RiskRecoveryPolicy,
                               reporter: ProgressReporter | None,
                               label: str = "",
                               stop_event: asyncio.Event | None = None,
                               cancel_event: threading.Event | None = None,
                               remaining: int = 0) -> tuple[bool, float]:
    """等待风控解除：每轮先等待，再用一个待下载曲目做探针。

    返回 (是否解除, 累计等待秒数)。等待期间不发请求；一个退避周期只发一个探针。
    探针项自身的不可恢复错误（AuthError / 非 retryable ApiError）会换下一项，
    全部耗尽后原样抛出；NetworkError 与可重试 ApiError 按瞬态继续等待。
    """
    if not probe_track_ids:
        return True, 0.0
    started = time.monotonic()
    attempt = 0
    candidates = list(probe_track_ids)
    while True:
        attempt += 1
        wait = policy.wait_for(attempt)
        suffix = f"，剩余 {remaining} 项待恢复" if remaining else ""
        _note(reporter, f"  ⏳ {label}风控中，{wait:.0f}s 后探测恢复"
                        f"（第 {attempt} 次{suffix}）…")
        if not await _sleep_with_stop(wait, stop_event, cancel_event):
            _note(reporter, f"  ⏹ {label}已停止等待风控恢复。")
            return False, time.monotonic() - started
        if (stop_event is not None and stop_event.is_set()) or \
                (cancel_event is not None and cancel_event.is_set()):
            return False, time.monotonic() - started
        if policy.max_duration > 0 and \
                time.monotonic() - started >= policy.max_duration:
            _note(reporter, f"  ⏰ {label}等待风控解除超过 "
                            f"{policy.max_duration:.0f}s，停止自动恢复，"
                            "可稍后 resume。")
            return False, time.monotonic() - started
        # 一个退避周期只发一个探针；成功即视为风控解除。
        while candidates:
            probe_id = candidates[0]
            try:
                await source.get_track(probe_id)
                waited = (0.0 if policy.initial_wait <= 0 and attempt == 1
                          else time.monotonic() - started)
                _note(reporter, f"  ✓ {label}风控已解除"
                                f"（等待 {waited:.0f}s），继续下载。")
                return True, waited
            except RiskControlError:
                break            # 仍风控：进入下一轮等待
            except (NetworkError, ApiError) as e:
                if isinstance(e, ApiError) and not e.retryable:
                    raise
                _note(reporter, f"  {label}探针瞬态失败（{e.category}），继续等待。")
                break
            except AuthError:
                _note(reporter, f"  {label}探针项不可恢复（{probe_id}），"
                                "改用下一项。")
                candidates.pop(0)
            except CancelledByUser:
                raise


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
            # 风控的 retryable 仅表示可以在恢复后继续；本函数内仍立即停止，
            # 不能把即时重试变成持续冲击。自动恢复由上层 RiskRecoveryPolicy
            # 以「等待 + 单探针」的循环处理。
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
                 cancel_event: threading.Event | None = None,
                 risk_recovery: RiskRecoveryPolicy | None = None):
        self._source = source
        self._sink = sink
        self._download_dir = download_dir
        self._retry = retry or RetryPolicy()
        self._store = store
        self._cancel_event = cancel_event
        self._risk_recovery = risk_recovery or RiskRecoveryPolicy()

    async def execute(self, target: str, quality: Quality,
                      reporter: ProgressReporter | None = None) -> str:
        track_id = parse_track_id(target)
        holder: dict[str, DownloadTask | None] = {"task": None}

        async def _do() -> str:
            track: Track = await _get_track(self._source, track_id, quality)
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
            await _write_media(
                self._sink, url=play.url, track_id=track_id, quality=quality,
                target_path=target_path, reporter=reporter,
                cancel=self._cancel_event, progress_sink=self._progress_sink(task),
            )
            if task is not None and task.id is not None:
                await self._store_call(reporter, self._store.mark_done,
                                       task.id, target_path)
            return target_path

        while True:
            try:
                return await _run_with_retry(_do, self._retry, reporter)
            except RiskControlError as e:
                if (not self._risk_recovery.enabled
                        or (self._cancel_event is not None
                            and self._cancel_event.is_set())):
                    await self._fail(holder["task"], e, reporter)
                    raise
                try:
                    recovered, _waited = await _await_risk_recovery(
                        self._source, [track_id], self._risk_recovery,
                        reporter, label="单曲 ",
                        cancel_event=self._cancel_event,
                    )
                except AuthError:
                    auth = AuthError("单曲任务不可恢复（权限或登录问题）。")
                    await self._fail(holder["task"], auth, reporter)
                    raise
                if not recovered:
                    await self._fail(holder["task"], e, reporter)
                    raise
                # 风控解除：重跑整条下载链（任务重新 prepare 为 pending）。
                continue
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
    risk_control: str | None = None
    deferred: int = 0
    recovered: bool = False          # 是否风控后自动恢复并完成
    risk_wait_seconds: float = 0.0   # 自动恢复累计等待（秒）

    def summary(self) -> str:
        line = (f"专辑《{self.album_title}》：下载 {len(self.downloaded)}，"
                f"跳过 {len(self.skipped)}，失败 {len(self.failed)}。")
        if self.risk_control:
            line += (f"（平台风控已熔断，{self.deferred} 项待恢复："
                     f"{self.risk_control}）")
        elif self.recovered:
            line += (f"（风控后自动恢复，等待 {self.risk_wait_seconds:.0f}s 后完成）")
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
                 cancel_event: threading.Event | None = None,
                 risk_recovery: RiskRecoveryPolicy | None = None):
        self._source = source
        self._sink = sink
        self._download_dir = download_dir
        # APK 连续失败按专辑集序计数；并发完成顺序不稳定，因此启用该能力时
        # 强制串行。未声明此能力的 WEB/PC/Chrome 保持原并发行为。
        self._concurrency = (
            1 if int(getattr(source, "max_consecutive_failures", 0)) > 0
            else max(1, concurrency)
        )
        self._retry = retry or RetryPolicy()
        self._store = store
        self._stop_event = stop_event
        self._cancel_event = cancel_event
        self._risk_recovery = risk_recovery or RiskRecoveryPolicy()

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
        self._apply_failures(result, failures)
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
        self._apply_failures(result, failures)
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

        login_failures = [
            (item, error) for item, error in failures
            if isinstance(error, LoginRequiredError)
        ]
        if login_failures:
            await self._requeue_items([item for item, _error in login_failures], reporter)
            _note(reporter, "APK 登录态缺失或已失效，已终止本批次；剩余任务保留待恢复。")
            raise login_failures[0][1]

        consecutive_failures = [
            (item, error) for item, error in failures
            if isinstance(error, ConsecutiveFailureError)
        ]
        if consecutive_failures:
            raise consecutive_failures[0][1]

        # 风控不是普通的逐项失败：首个信号出现后整批熔断。若启用了自动恢复，
        # 进入「等待 → 单探针 → 解除后继续」循环（解除后按原并发继续剩余项，
        # 期间可能再次风控则继续循环）；否则维持原语义，禁止失败收尾轮自动
        # 重新冲击受保护接口，任务保持 retryable 供人工 resume。
        while self._risk_recovery.enabled:
            risk_items = [item for item, error in failures
                          if isinstance(error, RiskControlError)]
            if not risk_items or self._is_stopping():
                break
            try:
                recovered, waited = await _await_risk_recovery(
                    self._source,
                    [item.track.track_id for item in risk_items],
                    self._risk_recovery, reporter,
                    stop_event=self._stop_event,
                    cancel_event=self._cancel_event,
                    remaining=len(risk_items),
                )
            except AuthError:
                # 剩余项全部不可恢复（无权限/登录问题），转为非重试失败。
                for item in risk_items:
                    if item.task and item.task.id is not None:
                        await self._store_call(
                            reporter, self._store.mark_failed,
                            item.task.id, "auth",
                            "剩余任务不可恢复（权限或登录问题）。", False,
                        )
                return [(item, AuthError("剩余任务不可恢复（权限或登录问题）。"))
                        for item in risk_items]
            if not recovered:
                if self._is_stopping():
                    await self._requeue_items(risk_items, reporter)
                break
            result.recovered = True
            result.risk_wait_seconds += waited
            await self._requeue_items(risk_items, reporter)
            _note(reporter, f"风控解除，继续下载剩余 {len(risk_items)} 项。")
            more = await self._run_batch(risk_items, quality, album_dir, width,
                                         total, reporter, result)
            failures = [pair for pair in failures
                        if not isinstance(pair[1], RiskControlError)] + more

        if any(isinstance(error, RiskControlError) for _item, error in failures):
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
        login_error: list[LoginRequiredError] = []
        max_consecutive = int(getattr(self._source, "max_consecutive_failures", 0))
        consecutive = 0
        consecutive_error: list[ConsecutiveFailureError] = []

        async def worker(item: _AlbumWorkItem) -> None:
            nonlocal consecutive
            async with sem:
                at = item.track
                if login_error:
                    failures.append((item, login_error[0]))
                    return
                if consecutive_error:
                    return
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
                    consecutive = 0
                    if item.task and item.task.id is not None:
                        await self._store_call(reporter, self._store.mark_done,
                                               item.task.id, path)
                    _note(reporter, f"  ✓ {label}")
                except CancelledByUser:
                    await self._requeue_items([item], reporter)
                    _note(reporter, f"  ↷ {label} — 已停止，保留待恢复")
                except XdlError as e:
                    if isinstance(e, LoginRequiredError):
                        if not login_error:
                            login_error.append(e)
                            _note(reporter, f"  ✗ {label} — {e}；终止本批次")
                        failures.append((item, e))
                        return
                    if isinstance(e, RiskControlError) and not risk_error:
                        risk_error.append(e)
                    if item.task and item.task.id is not None:
                        await self._store_call(reporter, self._store.mark_failed,
                                               item.task.id, e.category, str(e),
                                               e.retryable)
                    if not isinstance(e, RiskControlError):
                        _note(reporter, f"  ✗ {label} — {e}")
                    failures.append((item, e))
                    if max_consecutive and not isinstance(e, RiskControlError):
                        consecutive += 1
                        if consecutive >= max_consecutive and not consecutive_error:
                            stop = ConsecutiveFailureError(
                                f"APK 连续 {consecutive} 集下载失败，已终止本批次"
                                f"（最后错误：{e}）。"
                            )
                            consecutive_error.append(stop)
                            failures.append((item, stop))
                            _note(reporter, f"  ■ {stop} 后续任务保留待恢复。")

        await asyncio.gather(*(worker(item) for item in work))
        return failures

    @staticmethod
    def _apply_failures(
        result: AlbumResult,
        failures: list[tuple[_AlbumWorkItem, XdlError]],
    ) -> None:
        """把逐项错误收敛成用户结果；同一批次的风控只保留一个原因。"""
        risk_failures = [
            (item, error) for item, error in failures
            if isinstance(error, RiskControlError)
        ]
        result.failed = [
            (item.track, str(error)) for item, error in failures
            if not isinstance(error, RiskControlError)
        ]
        if risk_failures:
            result.risk_control = str(risk_failures[0][1])
            result.deferred = len(risk_failures)

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
        track = await _get_track(self._source, at.track_id, quality)
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
        await _write_media(
            self._sink, url=play.url, track_id=at.track_id, quality=quality,
            target_path=target_path, reporter=None, cancel=self._cancel_event,
            progress_sink=self._progress_sink(task),
            expected_total=task.total_bytes if task else 0,
        )
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
                 cancel_event: threading.Event | None = None,
                 risk_recovery: RiskRecoveryPolicy | None = None):
        self._source = source
        self._sink = sink
        self._download_dir = download_dir
        self._store = store
        self._concurrency = max(1, concurrency)
        self._retry = retry or RetryPolicy()
        self._stop_event = stop_event
        self._cancel_event = cancel_event
        self._risk_recovery = risk_recovery or RiskRecoveryPolicy()

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
            risk_recovery=self._risk_recovery,
        )
        results: list[AlbumResult] = []
        _note(reporter, "正在初始化音源会话并准备首个待恢复任务…")
        await self._source.open()
        try:
            for album_index, (album_id, title, _count) in enumerate(albums):
                _note(reporter, f"正在恢复专辑《{title}》…")
                tasks = await self._store_call(reporter, self._store.pending_tasks,
                                               album_id, default=[])
                if not tasks:
                    continue
                total = await self._store_call(reporter, self._store.album_total,
                                               album_id, default=0)
                merged = AlbumResult(title)
                quality_groups = list(self._by_quality(tasks).items())
                for group_index, (quality_value, group) in enumerate(quality_groups):
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
                    if partial.recovered:
                        merged.recovered = True
                        merged.risk_wait_seconds += partial.risk_wait_seconds
                    if partial.risk_control:
                        merged.risk_control = partial.risk_control
                        merged.deferred += partial.deferred
                        merged.deferred += sum(
                            len(rest) for _, rest in quality_groups[group_index + 1:]
                        )
                        break
                results.append(merged)
                if merged.risk_control:
                    merged.deferred += sum(
                        count for _, _, count in albums[album_index + 1:]
                    )
                    break
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
