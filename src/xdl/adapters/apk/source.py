"""独立 APK Source；不读取浏览器 Cookie/Profile。"""
from __future__ import annotations

import asyncio

from ...domain import Album, AlbumTrack, PlayUrl, Quality, Track
from ...errors import ConfigError, LoginRequiredError


class ApkSource:
    def __init__(self, client, *, max_consecutive_failures: int = 3):
        self.client = client
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))

    async def open(self) -> None:
        status = self.client.auth_status()
        if not status.get("authenticated"):
            raise LoginRequiredError(
                "APK 协议尚未登录，请先在页面点击“APK 登录”完成登录。"
            )
        await asyncio.to_thread(self.client.open)

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)

    async def get_track(self, track_id: str) -> Track:
        return await self.get_track_for_quality(track_id, Quality.STANDARD)

    async def get_track_for_quality(self, track_id: str, quality: Quality) -> Track:
        value = await asyncio.to_thread(self.client.resolve_track, str(track_id), quality.value)
        kind = "M4A_64" if ".m4a" in value["url"].lower() else "MP3_64"
        return Track(track_id=str(track_id), title=value["title"],
                     play_urls=[PlayUrl(kind, value["url"], value["file_size"])],
                     is_paid=value["is_paid"], is_authorized=True)

    def resolve_track_sync(self, track_id: str, quality: Quality) -> Track:
        value = self.client.resolve_track(str(track_id), quality.value)
        kind = "M4A_64" if ".m4a" in value["url"].lower() else "MP3_64"
        return Track(track_id=str(track_id), title=value["title"],
                     play_urls=[PlayUrl(kind, value["url"], value["file_size"])],
                     is_paid=value["is_paid"], is_authorized=True)

    async def get_album(self, album_id: str) -> Album:
        return await asyncio.to_thread(self._get_album_sync, str(album_id))

    def _get_album_sync(self, album_id: str) -> Album:
        first = self.client.album_download_page(album_id, 1, 1)
        rows = list(first["tracks"])
        for page in range(2, first["maxPageId"] + 1):
            rows.extend(self.client.album_download_page(album_id, page, 1)["tracks"])
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

    def interactive_login(self) -> str:
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
