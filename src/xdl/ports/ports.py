# -*- coding: utf-8 -*-
"""端口（抽象接口，见 docs/architecture.md §6）。

用 Protocol 描述核心需要的外部能力；适配器实现它们。
MVP 只立了单曲下载链路用到的端口，其余（TaskStore/RateLimiter/HookBus…）
待对应阶段再加。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain import Track, Album


@runtime_checkable
class Decoder(Protocol):
    """将平台返回的加密音频地址解码为可直接使用的 URL。"""
    def decode(self, encrypted_url: str) -> str: ...


@runtime_checkable
class Source(Protocol):
    """音源：获取单曲与专辑（后续扩展搜索）。"""
    def get_track(self, track_id: str) -> Track: ...
    def get_album(self, album_id: str) -> Album: ...

    # 批量会话：open/close 之间复用同一底层连接（如长驻浏览器），
    # 避免逐曲重复建链。无状态实现可空实现。
    def open(self) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class MediaSink(Protocol):
    """输出：把 URL 落盘（含进度回报、原子落盘）。"""
    def write(self, url: str, target_path: str, reporter: "ProgressReporter") -> None: ...


@runtime_checkable
class ProgressReporter(Protocol):
    """向前端回报进度（由前端实现）。"""
    def start(self, title: str, total: int) -> None: ...
    def update(self, done: int, total: int) -> None: ...
    def finish(self, path: str) -> None: ...
    def note(self, msg: str) -> None: ...      # 批量场景的逐条说明（专辑进度/跳过/失败等）
