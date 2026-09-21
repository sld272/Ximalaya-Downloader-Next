"""独立 APK Source；不读取浏览器 Cookie/Profile。"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import datetime, timezone

from ...domain import Album, AlbumTrack, PlayUrl, Quality, Track
from ...errors import ConfigError, LoginRequiredError, XdlError


class ApkSource:
    def __init__(self, client, *, max_consecutive_failures: int = 3,
                 risk_recorder=None):
        self.client = client
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))
        # APK 走的是 mobile/download/* 这一族接口，额度与 PC/HTTP 各算各的。
        # 没有观测就只能靠任务库的完成时刻倒推撞线过程，连请求间隔都拿不到。
        self._risk_recorder = risk_recorder
        self._session_id = str(uuid.uuid4())
        self._request_index = 0
        self._index_lock = threading.Lock()
        # mobile/download/* 端点有配额；解析路径与下载线程里的 URL 刷新路径
        # （ApkMediaSink 失效重解析）都必须过这道串行闸，否则下载并发会
        # 放大成对受保护接口的并发冲击。
        self._resolve_lock = threading.Lock()

    def _next_index(self) -> int:
        with self._index_lock:
            self._request_index += 1
            return self._request_index

    def _record(self, track_id: str, started: float, outcome: str,
                ret, msg, request_index: int) -> None:
        if self._risk_recorder is None:
            return
        try:
            self._risk_recorder.record(
                track_id=str(track_id),
                elapsed_ms=round((time.perf_counter() - started) * 1000),
                outcome=outcome,
                ret=ret,
                msg=str(msg or ""),
                in_flight=1,
                started_at=datetime.now(timezone.utc).isoformat(),
                request_index=request_index,
                session_id=self._session_id,
                authenticated=bool(getattr(self.client, "uid", "")),
                backend="apk",
            )
        except OSError:
            # 观测写盘失败不该影响下载本身；但签名类错误要照常炸出来
            pass

    async def open(self) -> None:
        status = self.client.auth_status()
        if not status.get("authenticated"):
            raise LoginRequiredError(
                "APK 协议尚未登录，请先在页面点击“APK 登录”完成登录。"
            )
        await asyncio.to_thread(self.client.open)

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)

    def close_sync(self) -> None:
        """SynchronouslyClosableSource 端口：Facade 的同步 close() 经此释放
        client 的长驻连接，无需伸手进适配器内部（.client.close()）。"""
        self.client.close()

    async def get_track(self, track_id: str) -> Track:
        return await self.get_track_for_quality(track_id, Quality.STANDARD)

    async def get_track_for_quality(self, track_id: str, quality: Quality) -> Track:
        return await asyncio.to_thread(self.resolve_track_sync, track_id, quality)

    def resolve_track_sync(self, track_id: str, quality: Quality) -> Track:
        with self._resolve_lock:
            value = self._guarded(str(track_id), self.client.resolve_track,
                                  str(track_id), quality.value)
        kind = "M4A_64" if ".m4a" in value["url"].lower() else "MP3_64"
        return Track(track_id=str(track_id), title=value["title"],
                     play_urls=[PlayUrl(kind, value["url"], value["file_size"])],
                     is_paid=value["is_paid"], is_authorized=True)

    def _guarded(self, label: str, call, *args):
        """跑一次受保护请求并落观测；异常按 category 记录后原样抛出。"""
        started = time.perf_counter()
        index = self._next_index()
        try:
            result = call(*args)
        except XdlError as error:
            self._record(label, started, error.category,
                         getattr(error, "ret", None), str(error), index)
            raise
        except Exception as error:
            self._record(label, started, "unexpected", None,
                         type(error).__name__, index)
            raise
        self._record(label, started, "success", None, None, index)
        return result

    async def get_album(self, album_id: str) -> Album:
        return await asyncio.to_thread(self._get_album_sync, str(album_id))

    def _album_page(self, album_id: str, page: int) -> dict:
        return self._guarded(f"album:{album_id}:p{page}",
                             self.client.album_download_page, album_id, page, 1)

    def _get_album_sync(self, album_id: str) -> Album:
        # 清单页同样打受保护接口，必须计入观测——否则统计出来的请求节奏会偏低。
        first = self._album_page(album_id, 1)
        rows = list(first["tracks"])
        for page in range(2, first["maxPageId"] + 1):
            rows.extend(self._album_page(album_id, page)["tracks"])
        tracks = []
        for fallback, item in enumerate(rows, 1):
            track_id = str(item.get("trackId") or item.get("dataId") or item.get("id") or "")
            if not track_id:
                continue
            tracks.append(AlbumTrack(track_id=track_id,
                                     title=str(item.get("title") or item.get("trackTitle") or f"Track {track_id}"),
                                     index=int(item.get("orderNo") or item.get("index") or fallback),
                                     is_paid=bool(item.get("isPaid", False))))
        album = first["album"]
        title = str(album.get("title") or album.get("albumTitle") or f"Album {album_id}")
        return Album(album_id=album_id, title=title,
                     total=first["totalCount"] or len(tracks), tracks=tracks)

    def interactive_login(self, **_wait_options) -> str:
        raise ConfigError("APK 登录是多阶段流程，请在 WebUI 中完成安全验证和登录。")

    async def inspect_storage(self) -> dict:
        return {"backend": "apk", "storage": "isolated", "keys": []}

    def auth_status(self):
        return self.client.auth_status()

    def login_config(self):
        return self.client.login_config()

    def send_sms(self, mobile: str, fds_otp: dict):
        return self.client.send_sms(mobile, fds_otp)

    def verify_sms(self, code: str):
        return self.client.verify_sms(code)

    def login_password(self, account: str, password: str, mode: str,
                       fds_otp: dict):
        return self.client.login_password(account, password, mode, fds_otp)

    def logout(self):
        self.client.logout()

    def switch_account(self, uid: str):
        return self.client.switch_account(uid)

    def delete_account(self, uid: str):
        return self.client.delete_account(uid)
