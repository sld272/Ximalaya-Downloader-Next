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


@dataclass
class AlbumTrack:
    """专辑内一集的清单条目（不含 playUrl；下载时再逐集解析）。"""
    track_id: str
    title: str
    index: int            # 专辑内 1 基序号
    is_paid: bool = False


@dataclass
class Album:
    """专辑及其曲目清单。"""
    album_id: str
    title: str
    total: int = 0                                  # 平台声明的曲目总数
    tracks: list[AlbumTrack] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """已取到的曲目数是否覆盖平台声明的总数（未登录时常只取到第一页）。"""
        return self.total <= 0 or len(self.tracks) >= self.total

    def select_range(self, start: int | None, end: int | None) -> list[AlbumTrack]:
        """按 1 基序号区间（闭区间）筛选曲目；start/end 为 None 表示不设下/上界。"""
        lo = start if start is not None else 1
        hi = end if end is not None else None
        return [t for t in self.tracks
                if t.index >= lo and (hi is None or t.index <= hi)]


# 输入解析：从链接或纯数字中提取 ID（放在领域层，保证各前端一致）
_SOUND_RE = re.compile(r"/sound/(\d+)")
_ALBUM_RE = re.compile(r"/album/(\d+)")
# 末尾数字（允许其后跟查询串），用于 .../sound 之外的链接形态
_TAIL_RE = re.compile(r"(\d+)(?:\?|$)")
_NUM_RE = re.compile(r"\d+")


def parse_track_id(raw: str) -> str:
    raw = raw.strip()
    if raw.isdigit():
        return raw
    m = _SOUND_RE.search(raw) or _TAIL_RE.search(raw)
    if m:
        return m.group(1)
    nums = _NUM_RE.findall(raw)               # 兜底：路径中最后一个数字段（如带尾斜杠）
    if nums:
        return nums[-1]
    raise ValueError(f"无法从输入中解析 trackId: {raw!r}")


def parse_album_id(raw: str) -> str:
    raw = raw.strip()
    if raw.isdigit():
        return raw
    m = _ALBUM_RE.search(raw) or _TAIL_RE.search(raw)
    if m:
        return m.group(1)
    nums = _NUM_RE.findall(raw)               # 兜底：路径中最后一个数字段（如 .../{id}/）
    if nums:
        return nums[-1]
    raise ValueError(f"无法从输入中解析 albumId: {raw!r}")


_RANGE_RE = re.compile(r"^\s*(\d+)?\s*-\s*(\d+)?\s*$")


def parse_range(raw: str | None) -> tuple[int | None, int | None]:
    """解析下载区间：'1-20' / '5-' / '-10' / '7'（单集）。

    返回 (start, end) 闭区间，None 表示不设界。非法输入抛 ValueError。
    """
    if raw is None:
        return None, None
    raw = raw.strip()
    if not raw:
        return None, None
    if raw.isdigit():                       # 单集，如 '7' → (7, 7)
        n = int(raw)
        return n, n
    m = _RANGE_RE.match(raw)
    if not m or (m.group(1) is None and m.group(2) is None):
        raise ValueError(f"无法解析区间: {raw!r}（示例：1-20 / 5- / -10 / 7）")
    start = int(m.group(1)) if m.group(1) else None
    end = int(m.group(2)) if m.group(2) else None
    if start is not None and end is not None and start > end:
        raise ValueError(f"区间起点大于终点: {raw!r}")
    return start, end
