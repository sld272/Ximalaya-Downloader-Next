# -*- coding: utf-8 -*-
"""门面（库的公开 API，见 docs/architecture.md §11）。

各前端（CLI / 未来 GUI/Web）只依赖这一层。公开方法保持**同步**签名，内部用
asyncio 驱动异步解析/并发（`asyncio.run` 收口），前端零改动。
"""
from __future__ import annotations

import asyncio
import os
import signal
import threading
from collections.abc import Callable

from ..domain import Quality, parse_range
from ..errors import XdlError
from ..settings import Settings
from .usecases import (DownloadTrackUseCase, DownloadAlbumUseCase, AlbumResult,
                       ResumeUseCase, RetryPolicy)


class Facade:
    def __init__(self, source, sink, settings: Settings, store=None,
                 store_factory: Callable[[], object] | None = None):
        self._source = source
        self._sink = sink
        self._settings = settings
        self._store = store
        self._store_factory = store_factory

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
                       reporter=None, cancel: threading.Event | None = None) -> str:
        """下载单个音频，返回落盘路径。`cancel` 供外部触发停止。"""
        return asyncio.run(self._download_track(target, quality, reporter, cancel))

    def download_album(self, target: str, quality: str | None = None,
                       range_: str | None = None, reporter=None,
                       cancel: threading.Event | None = None) -> AlbumResult:
        """并发批量下载专辑，返回下载汇总。range_ 形如 '1-20' / '5-' / '-10' / '7'。

        `cancel` 供 GUI/TUI 从外部触发优雅停止（等价于 Ctrl-C）。
        """
        return asyncio.run(self._download_album(target, quality, range_, reporter,
                                                cancel))

    def resume(self, reporter=None,
               cancel: threading.Event | None = None) -> list[AlbumResult]:
        """继续任务库中未完成的专辑下载。`cancel` 供外部触发优雅停止。"""
        return asyncio.run(self._resume(reporter, cancel))

    def all_tasks(self):
        """只读：返回任务库中的全部任务（供 TUI/GUI 渲染面板）。无任务库时返回 []。"""
        store = self._task_store()
        return store.all_tasks() if store is not None else []

    # ---- 内部异步实现 ----
    async def _download_track(self, target, quality, reporter, cancel=None) -> str:
        q = Quality(quality or self._settings.default_quality)
        usecase = DownloadTrackUseCase(self._source, self._sink,
                                       self._settings.download_dir,
                                       retry=self._retry_policy(),
                                       store=self._optional_store(),
                                       cancel_event=cancel)
        await self._source.open()
        try:
            return await usecase.execute(target, q, reporter)
        finally:
            await self._source.close()

    async def _download_album(self, target, quality, range_, reporter,
                              cancel=None) -> AlbumResult:
        q = Quality(quality or self._settings.default_quality)
        start, end = parse_range(range_)
        stop_event = asyncio.Event()
        cancel_event = threading.Event()
        store = self._task_store()
        cleanup_signal = self._install_sigint_handler(stop_event, cancel_event)
        watcher = self._watch_external_cancel(cancel, stop_event, cancel_event)
        usecase = DownloadAlbumUseCase(self._source, self._sink,
                                       self._settings.download_dir,
                                       concurrency=self._settings.max_concurrency,
                                       retry=self._retry_policy(),
                                       store=store,
                                       stop_event=stop_event,
                                       cancel_event=cancel_event)
        # 全程复用一个 Chrome 会话（共享上下文里按需开 page 并发解析）
        try:
            await self._source.open()
            try:
                result = await usecase.execute(target, q, start, end, reporter)
            finally:
                await self._source.close()
            if stop_event.is_set():
                result.stopped = True
            return result
        finally:
            self._cancel_task(watcher)
            cleanup_signal()

    async def _resume(self, reporter, cancel=None) -> list[AlbumResult]:
        store = self._task_store()
        if store is None:
            return []
        stop_event = asyncio.Event()
        cancel_event = threading.Event()
        cleanup_signal = self._install_sigint_handler(stop_event, cancel_event)
        watcher = self._watch_external_cancel(cancel, stop_event, cancel_event)
        usecase = ResumeUseCase(self._source, self._sink,
                                self._settings.download_dir, store,
                                concurrency=self._settings.max_concurrency,
                                retry=self._retry_policy(),
                                stop_event=stop_event,
                                cancel_event=cancel_event)
        try:
            result = await usecase.execute(reporter)
            if stop_event.is_set():
                for album_result in result:
                    album_result.stopped = True
            return result
        finally:
            self._cancel_task(watcher)
            cleanup_signal()

    def _watch_external_cancel(self, cancel: threading.Event | None,
                               stop_event: asyncio.Event,
                               cancel_event: threading.Event):
        """把外部 threading.Event 桥接到内部的优雅停止机制。"""
        if cancel is None:
            return None

        async def _watch() -> None:
            try:
                while not cancel.is_set():
                    if stop_event.is_set():
                        return
                    await asyncio.sleep(0.1)
                stop_event.set()
                cancel_event.set()
            except asyncio.CancelledError:
                pass

        return asyncio.ensure_future(_watch())

    @staticmethod
    def _cancel_task(task) -> None:
        if task is not None:
            task.cancel()

    def _task_store(self):
        if self._store is None and self._store_factory is not None:
            self._store = self._store_factory()
        return self._store

    def _optional_store(self):
        """尽力拿任务库；坏了就返回 None，让单曲下载照常进行（记不进面板而已）。"""
        try:
            return self._task_store()
        except XdlError:
            return None

    def _install_sigint_handler(self, stop_event: asyncio.Event,
                                cancel_event: threading.Event):
        loop = asyncio.get_running_loop()

        def request_stop() -> None:
            if stop_event.is_set():
                signal.signal(signal.SIGINT, signal.default_int_handler)
                os.kill(os.getpid(), signal.SIGINT)
                return
            stop_event.set()
            cancel_event.set()

        try:
            loop.add_signal_handler(signal.SIGINT, request_stop)
            return lambda: loop.remove_signal_handler(signal.SIGINT)
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                previous = signal.getsignal(signal.SIGINT)
                signal.signal(
                    signal.SIGINT,
                    lambda _sig, _frame: loop.call_soon_threadsafe(request_stop),
                )
            except (RuntimeError, ValueError):
                return lambda: None
            return lambda: signal.signal(signal.SIGINT, previous)
