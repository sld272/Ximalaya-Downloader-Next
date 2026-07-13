# -*- coding: utf-8 -*-
"""专辑曲目清单（免签公开接口）—— `ChromeSource` 与 `HttpSource` 共享的实现。

走「非 v1」getTracksList 接口：纯 HTTP、匿名可取、只回曲目元信息（id/标题/序号，
不含 playUrl），每页固定 30 条。详见 docs/architecture.md §7.2 的备注。
"""
from __future__ import annotations

import time

import requests

from ..config import platform
from ..domain import Album, AlbumTrack
from ..errors import NetworkError, ApiError

_MAX_PAGES = 2000
_LIST_TIMEOUT = 30


def fetch_album(album_id: str) -> Album:
    """翻页抓取专辑曲目清单。"""
    headers = {
        "User-Agent": platform.UA,
        "Referer": platform.ALBUM_URL.format(album_id=album_id),
    }
    tracks: list[AlbumTrack] = []
    title: str | None = None
    total = 0
    for page_num in range(1, _MAX_PAGES + 1):
        try:
            resp = requests.get(
                platform.TRACKS_LIST_URL,
                params={"albumId": album_id, "pageNum": page_num, "sort": 0},
                headers=headers, timeout=_LIST_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json()
        except requests.RequestException as e:
            raise NetworkError(f"获取专辑曲目清单失败: {e}") from e
        if body.get("ret") != 200 or not body.get("data"):
            raise ApiError(
                f"获取专辑曲目清单失败（ret={body.get('ret')} msg={body.get('msg')}）。",
                ret=body.get("ret"),
            )
        data = body["data"]
        total = int(data.get("trackTotalCount") or 0)
        batch = data.get("tracks") or []
        if not batch:
            break
        for t in batch:
            tracks.append(AlbumTrack(
                track_id=str(t.get("trackId")),
                title=t.get("title") or str(t.get("trackId")),
                index=int(t.get("index") or len(tracks) + 1),
                is_paid=bool(t.get("isPaid")),
            ))
            if title is None and t.get("albumTitle"):
                title = t["albumTitle"]
        if total and len(tracks) >= total:
            break
        time.sleep(0.3)
    if not tracks:
        raise ApiError("专辑无可下载曲目或不存在。")
    return Album(album_id=album_id, title=title or album_id,
                 total=total or len(tracks), tracks=tracks)