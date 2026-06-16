# -*- coding: utf-8 -*-
"""领域模型（纯对象，无 I/O）。

见 docs/architecture.md §5。当前 MVP 实现单曲下载所需的最小集合：
Quality（含降级协商）、PlayUrl、Track。DownloadTask 状态机等留待任务引擎阶段。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Quality(Enum):
    """音质等级，含「降级协商」：请求音质不可用时按偏好顺序回退。"""
    HIGH = "high"
    STANDARD = "standard"
    LOW = "low"

    @property
    def _preference(self) -> list[str]:
        # 平台音质类型按各等级的偏好排序
        return {
            Quality.HIGH: ["M4A_128", "MP3_64", "AI_128", "MP3_32"],
            Quality.STANDARD: ["MP3_64", "M4A_128", "AI_128", "MP3_32"],
            Quality.LOW: ["MP3_32", "MP3_64", "AI_128", "M4A_128"],
        }[self]

    def negotiate(self, available_types: list[str]) -> str | None:
        """在可用音质类型中按偏好挑选；都不匹配则取第一个可用。"""
        for t in self._preference:
            if t in available_types:
                return t
        return available_types[0] if available_types else None


@dataclass
class PlayUrl:
    """单个可播放资源。url 为已解码、可直接下载的地址。"""
    type: str
    url: str
    file_size: int = 0

    @property
    def is_m4a(self) -> bool:
        return self.type.startswith("M4A") or ".m4a" in self.url.lower()

    @property
    def ext(self) -> str:
        return ".m4a" if self.is_m4a else ".mp3"


@dataclass
class Track:
    """音频曲目。"""
    track_id: str
    title: str
    play_urls: list[PlayUrl] = field(default_factory=list)
    is_paid: bool = False
    is_authorized: bool = True

    def select(self, quality: Quality) -> PlayUrl | None:
        """按音质协商选出一个可用的 PlayUrl。"""
        by_type = {p.type: p for p in self.play_urls if p.url}
        chosen = quality.negotiate(list(by_type.keys()))
        return by_type.get(chosen) if chosen else None


# 输入解析：从链接或纯数字中提取 trackId（放在领域层，保证各前端一致）
_SOUND_RE = re.compile(r"/sound/(\d+)")
_TAIL_RE = re.compile(r"(\d+)(?:\?|$)")


def parse_track_id(raw: str) -> str:
    raw = raw.strip()
    if raw.isdigit():
        return raw
    m = _SOUND_RE.search(raw) or _TAIL_RE.search(raw)
    if m:
        return m.group(1)
    raise ValueError(f"无法从输入中解析 trackId: {raw!r}")
