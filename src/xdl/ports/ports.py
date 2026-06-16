# -*- coding: utf-8 -*-
"""端口（抽象接口，见 docs/architecture.md §6）。

用 Protocol 描述核心需要的外部能力；适配器实现它们。
MVP 只立了单曲下载链路用到的端口，其余（TaskStore/RateLimiter/HookBus…）
待对应阶段再加。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain import Track


@runtime_checkable
class Decoder(Protocol):
    """将平台返回的加密音频地址解码为可直接使用的 URL。"""
    def decode(self, encrypted_url: str) -> str: ...


@runtime_checkable
class Source(Protocol):
    """音源：获取单曲（后续扩展专辑 / 搜索）。"""
    def get_track(self, track_id: str) -> Track: ...


@runtime_checkable
class MediaSink(Protocol):
    """输出：把 URL 落盘（含进度回报、原子落盘）。"""
    def write(self, url: str, target_path: str, reporter: "ProgressReporter") -> None: ...


@runtime_checkable
class CookieJar(Protocol):
    """登录会话（storage_state）的读写持久化。"""
    def state_path(self) -> str | None: ...   # 存在则返回路径，否则 None
    def location(self) -> str: ...            # 用于写入的目标路径


@runtime_checkable
class ProgressReporter(Protocol):
    """向前端回报进度（由前端实现）。"""
    def start(self, title: str, total: int) -> None: ...
    def update(self, done: int, total: int) -> None: ...
    def finish(self, path: str) -> None: ...
