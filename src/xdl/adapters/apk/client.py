"""喜马拉雅 APK 9.5.1.3 HTTP 协议客户端。"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any, Callable

import requests

from ...errors import (ApiError, AuthError, DownloadLimitError,
                       LoginRequiredError, NetworkError, RiskControlError)
from .state import ApkStateStore

PASSPORT = "https://passport.ximalaya.com/"
MOBILE = "https://mobile.ximalaya.com/"
GEETEST_CAPTCHA_ID = "3723312ce42a04b5c0b40e605a882037"
CAPTCHA_FIELDS = {"captcha_id", "lot_number", "pass_token", "gen_time", "captcha_output"}
QUALITY_LEVEL = {"low": 0, "standard": 1, "high": 3}
SMS_RETRY_SECONDS = 180
PASSWORD_LOGIN_MODES = {"mobile", "email"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("***" if key.lower() in {"token", "faketoken", "mobile", "mobilecipher"} and item
                      else _redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _ret_error(result: dict[str, Any], fallback: str) -> None:
    try:
        ret = int(result.get("ret", -1))
    except (TypeError, ValueError):
        ret = -1
    if ret == 0:
        return
    msg = str(result.get("msg") or fallback)
    lower = msg.lower()
    if any(phrase in msg for phrase in ("体验会员下载上限", "已超过下载上限",
                                        "下载次数已达上限")):
        raise DownloadLimitError(msg)
    if ret in {1001, 3005, 31009} or any(word in lower for word in ("繁忙", "频繁", "验证码")):
        raise RiskControlError(msg, ret=ret)
    if ret in {401, 403} or any(word in lower for word in ("登录", "token")):
        raise LoginRequiredError(msg)
    if ret == 726 or any(word in msg for word in ("无下载权限", "暂无下载权限")):
        raise AuthError(msg)
    if "授权" in lower:
        raise AuthError(msg)
    raise ApiError(msg, ret=ret, retryable=False)


class ApkClient:
    def __init__(self, bridge, state: ApkStateStore, *, timeout: float = 30.0,
                 session: requests.Session | None = None,
                 clock_ms: Callable[[], int] | None = None):
        self.bridge = bridge
        self.state = state
        self.timeout = timeout
        self.session = session or requests.Session()
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.uid, self.token = state.load_auth()
        self.device_id = state.device_id
        self.xuid = state.load_xuid()
        self.encrypted_mobile = ""
        self.sms_key = ""
        self._last_sms: dict[str, float] = {}
        self._used_lots: set[str] = set()
        self._lock = threading.RLock()
        self._profile = {
            "userAgent": "ting_9.5.1(M2102J2SC,Android31)",
            "cookie": ("1&_device=android&{device_id}&9.5.1;channel=and-d10;"
                       "impl=com.ximalaya.ting.android;qimei36=;osversion=31;"
                       "device_model=M2102J2SC;XIM=;net-mode=WIFI;manufacturer=Xiaomi;"),
        }

    def open(self) -> None:
        self.bridge.open()
        if not self.xuid:
            self.xuid = self.bridge.create_xuid(self.device_id)
            self.state.save_xuid(self.xuid)

    def close(self) -> None:
        self.bridge.close()
        self.session.close()

    def auth_status(self) -> dict[str, Any]:
        return {"authenticated": bool(self.uid and self.token), "uid": self.uid,
                "backend": "apk", "device_id_suffix": self.device_id[-8:],
                "accounts": self.state.list_accounts()}

    def login_config(self) -> dict[str, Any]:
        return {"captcha_id": GEETEST_CAPTCHA_ID, "biz": 1,
                "captcha_fields": sorted(CAPTCHA_FIELDS)}

    def _cookie(self) -> str:
        value = self._profile["cookie"].format(device_id=self.device_id)
        if self.uid and self.token:
            value += f"1&_token={self.uid}&{self.token};"
        return value

    def _request(self, method: str, url: str, params: dict[str, Any] | None = None,
                 *, json_body: bool = False, headers: dict[str, str] | None = None) -> dict[str, Any]:
        request_headers = {
            "User-Agent": self._profile["userAgent"], "Cookie": self._cookie(),
            "Accept": "application/json, text/plain, */*", "Cookie2": "$version=1",
        }
        request_headers.update(headers or {})
        values = {key: str(value) for key, value in (params or {}).items() if value is not None}
        try:
            response = self.session.request(
                method, url, params=values if method == "GET" else None,
                json=values if method != "GET" and json_body else None,
                data=values if method != "GET" and not json_body else None,
                headers=request_headers, timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
        except requests.RequestException as exc:
            raise NetworkError(f"APK 请求失败: {exc}") from exc
        except ValueError as exc:
            raise ApiError("APK 接口未返回 JSON。", retryable=True) from exc
        if not isinstance(result, dict):
            raise ApiError("APK 接口返回格式无效。", retryable=True)
        return result

    def _nonce(self) -> str:
        result = self._request("GET", PASSPORT + f"mobile/nonce/{self.clock_ms()}")
        nonce = result.get("nonce")
        if not nonce:
            raise ApiError("APK nonce 获取失败。", retryable=True)
        return str(nonce)

    def _signed_post(self, path: str, values: dict[str, Any]) -> dict[str, Any]:
        normalized = {key: str(value) for key, value in values.items()}
        normalized["signature"] = self.bridge.sign(normalized)
        return self._request("POST", PASSPORT + path, normalized, json_body=True)

    def send_sms(self, mobile: str, fds_otp: dict[str, Any]) -> dict[str, Any]:
        if not re.fullmatch(r"\+?\d{6,18}", mobile):
            raise ApiError("手机号格式无效。")
        if not isinstance(fds_otp, dict) or not CAPTCHA_FIELDS.issubset(fds_otp):
            raise ApiError("GeeTest 安全验证字段不完整。")
        if str(fds_otp.get("captcha_id", "")).lower() != GEETEST_CAPTCHA_ID:
            raise ApiError("GeeTest captcha_id 与 APK 配置不一致。")
        lot = str(fds_otp["lot_number"])
        key = hashlib.sha256(mobile.encode()).hexdigest()
        with self._lock:
            if lot in self._used_lots:
                raise ApiError("本次安全验证已经使用，请重新验证。")
            elapsed = time.monotonic() - self._last_sms.get(key, 0)
            if elapsed < SMS_RETRY_SECONDS:
                remaining = max(1, int(SMS_RETRY_SECONDS - elapsed))
                raise RiskControlError(f"请求过于频繁，请 {remaining} 秒后再试。", ret=429)
            self._used_lots.add(lot)
            self._last_sms[key] = time.monotonic()
            self.encrypted_mobile = self.bridge.encrypt_mobile(mobile)
            result = self._signed_post("mobile/sms/v3/send", {
                "mobile": self.encrypted_mobile, "sendType": "1", "biz": "1",
                "nonce": self._nonce(),
                "fdsOtp": json.dumps(fds_otp, ensure_ascii=False, separators=(",", ":")),
            })
            _ret_error(result, "发送 APK 短信验证码失败。")
            return {"success": True, "ret": result.get("ret", 0),
                    "retry_after_seconds": SMS_RETRY_SECONDS}

    def verify_sms(self, code: str) -> dict[str, Any]:
        if not self.encrypted_mobile:
            raise ApiError("请先发送短信验证码。")
        if not re.fullmatch(r"\d{4,8}", code):
            raise ApiError("验证码格式无效。")
        with self._lock:
            verify = self._signed_post("mobile/sms/v2/verify", {
                "mobile": self.encrypted_mobile, "code": code, "nonce": self._nonce(),
            })
            _ret_error(verify, "APK 短信验证码校验失败。")
            self.sms_key = str(verify.get("bizKey") or verify.get("smsKey") or "")
            if not self.sms_key:
                raise ApiError("APK 验证响应缺少 smsKey。")
            login = self._signed_post("mobile/login/quick/v2", {
                "mobile": self.encrypted_mobile, "smsKey": self.sms_key,
                "nonce": self._nonce(),
            })
            _ret_error(login, "APK 快捷登录失败。")
            data = login.get("data") if isinstance(login.get("data"), dict) else {}
            self.uid = str(login.get("uid") or data.get("uid") or "")
            self.token = str(login.get("token") or data.get("token") or "")
            if not self.uid or not self.token:
                raise AuthError("APK 登录响应缺少 uid/token。")
            self.state.save_auth(self.uid, self.token)
            return self.auth_status()

    def login_password(self, account: str, password: str, mode: str,
                       fds_otp: dict[str, Any]) -> dict[str, Any]:
        """APK 账号密码登录；手机号与邮箱共用 pwd/v3 协议。"""
        account = account.strip()
        mode = mode.strip().lower()
        if mode not in PASSWORD_LOGIN_MODES:
            raise ApiError("密码登录方式必须是 mobile 或 email。")
        if mode == "mobile" and not re.fullmatch(r"\+?\d{6,18}", account):
            raise ApiError("手机号格式无效。")
        if mode == "email" and not re.fullmatch(
                r"[^\s@]+@[^\s@]+\.[^\s@]+", account):
            raise ApiError("邮箱格式无效。")
        if not password or len(password) > 128:
            raise ApiError("密码不能为空且不能超过 128 个字符。")
        if not isinstance(fds_otp, dict) or not CAPTCHA_FIELDS.issubset(fds_otp):
            raise ApiError("GeeTest 安全验证字段不完整。")
        if str(fds_otp.get("captcha_id", "")).lower() != GEETEST_CAPTCHA_ID:
            raise ApiError("GeeTest captcha_id 与 APK 配置不一致。")
        lot = str(fds_otp["lot_number"])
        with self._lock:
            if lot in self._used_lots:
                raise ApiError("本次安全验证已经使用，请重新验证。")
            self._used_lots.add(lot)
            values = {
                "account": self.bridge.encrypt_mobile(account),
                "password": self.bridge.encrypt_mobile(password),
                "nonce": self._nonce(),
                "fdsOtp": json.dumps(
                    fds_otp, ensure_ascii=False, separators=(",", ":"),
                ),
            }
            result = self._signed_post("mobile/login/pwd/v3", values)
            _ret_error(result, "APK 账号密码登录失败。")
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            self.uid = str(result.get("uid") or data.get("uid") or "")
            self.token = str(result.get("token") or data.get("token") or "")
            if not self.uid or not self.token:
                raise AuthError("APK 密码登录响应缺少 uid/token。")
            self.state.save_auth(self.uid, self.token)
            return self.auth_status()

    def logout(self) -> None:
        with self._lock:
            if self.uid:
                self.uid, self.token = self.state.delete_auth(self.uid)

    def switch_account(self, uid: str) -> dict[str, Any]:
        with self._lock:
            self.uid, self.token = self.state.switch_auth(str(uid).strip())
            return self.auth_status()

    def delete_account(self, uid: str) -> dict[str, Any]:
        with self._lock:
            self.uid, self.token = self.state.delete_auth(str(uid).strip())
            return self.auth_status()

    def _ticket(self, scene: str) -> str:
        self.open()
        return self.bridge.ticket(
            f"b=downloadTrack&s={scene}&u={self.uid or '0'}", self.xuid
        )

    def album_download_page(self, album_id: str, page: int, quality: int = 1) -> dict[str, Any]:
        if not album_id.isdigit():
            raise ApiError("albumId 必须是数字。")
        result = self._request(
            "GET", MOBILE + f"mobile/download/v1/album/{album_id}/{max(page, 1)}/true/ts-{self.clock_ms()}",
            {"albumId": album_id, "pageId": max(page, 1), "isAsc": "true",
             "device": "android", "trackQualityLevel": quality},
            headers={"x-tk": self._ticket("batch_download")},
        )
        _ret_error(result, "APK 批量下载清单获取失败。")
        tracks = result.get("tracks") if isinstance(result.get("tracks"), dict) else {}
        return {"album": result.get("album") if isinstance(result.get("album"), dict) else {},
                "tracks": tracks.get("list") if isinstance(tracks.get("list"), list) else [],
                "pageId": int(tracks.get("pageId") or page),
                "maxPageId": int(tracks.get("maxPageId") or 1),
                "totalCount": int(tracks.get("totalCount") or 0)}

    def track_download_info(self, track_id: str, quality: int) -> dict[str, Any]:
        if not track_id.isdigit():
            raise ApiError("trackId 必须是数字。")
        result = self._request(
            "GET", MOBILE + f"mobile/download/v2/track/{track_id}/ts-{self.clock_ms()}",
            {"trackId": track_id, "trackQualityLevel": quality, "device": "android"},
            headers={"x-tk": self._ticket("download")},
        )
        _ret_error(result, "APK 单集下载信息获取失败。")
        return result

    def resolve_track(self, track_id: str, quality_name: str) -> dict[str, Any]:
        quality = QUALITY_LEVEL[quality_name]
        result = self.track_download_info(track_id, quality)
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        url = next((str(data.get(key)) for key in
                    ("downloadAacUrl", "downloadUrl", "playPathAacv164", "playPathAacv224")
                    if isinstance(data.get(key), str) and str(data.get(key)).startswith(("http://", "https://"))), "")
        if not url:
            encrypted = data.get("downloadAacUrl") or data.get("downloadUrl")
            if encrypted:
                url = self.bridge.decrypt_download(
                    str(encrypted), int(data.get("downloadEncryptVersion") or 0)
                )
        if not url.startswith(("http://", "https://")):
            raise ApiError("APK 下载地址解密失败。", retryable=True)
        return {"track_id": track_id, "title": str(data.get("title") or data.get("trackTitle") or f"Track {track_id}"),
                "url": url, "file_size": int(data.get("fileSize") or data.get("downloadSize") or 0),
                "is_paid": bool(data.get("isPaid", False)), "raw": _redact(data)}
