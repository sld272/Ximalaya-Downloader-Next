# -*- coding: utf-8 -*-
"""实验性 HTTP 音源适配器。

它为特定 `baseInfo` 请求填充 `xm-sign`，并读取已持久化的登录 Cookie。签名、
认证、内容授权和服务端风控是彼此独立的判断：本模块不把任一项当作规避访问控制的
手段，也不会在缺少登录 token 时把匿名 Cookie 缓存为已登录会话。
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timezone

import requests

try:
    from curl_cffi import requests as cffi_requests
    _HAS_CURL_CFFI = True
except ImportError:
    cffi_requests = None
    _HAS_CURL_CFFI = False

from ..config import platform, sign as sign_conf
from ..domain import Album, PlayUrl, Track
from ..errors import (ApiError, AuthError, ConfigError, NetworkError,
                      RiskControlError, SignError)
from ..ports import Decoder, SignProvider
from ..risk import RiskEventRecorder
from .sign.cookies import (build_cookie_header, extract_cookies_from_profile,
                           load_cached_cookies, save_cookies, is_login_cookie)
from ._album_list import fetch_album as _fetch_album_list

# 与 ChromeSource 的风控判定保持一致：3005 是复用码，仅在 msg 含"系统繁忙"时
# 视为风控；否则按权限/鉴权处理。
_AUTH_RETS = {927}


def _raise_for_ret(ret, msg, *, authenticated: bool | None = None) -> None:
    """按 ret 语义把接口失败映射成对应的类型化异常。

    `1001` 有歧义：匿名访问付费内容也会回 `1001/系统繁忙`（不是真风控），而登录
    用户的频控熔断同样是 `1001`。本适配器据此分两种处理：
      - 当前 Cookie 里没有 `1&_token`（authenticated=False）：当作匿名拒绝，抛
        `ApiError` 让上层走错误重试链路提示用户登录，**不熔断整批**；
      - 已登录但回了 `1001`：才是真风控，按 `RiskControlError` 熔断。
    """
    msg_str = str(msg or "")
    is_busy = ret == 1001 or (ret == 3005 and "系统繁忙" in msg_str)
    if is_busy and authenticated is False:
        raise ApiError(
            f"未登录或匿名访问被拒（ret={ret} msg={msg}）。"
            "请先 `xdl login` 完成登录，再 `xdl refresh-cookies` 刷新登录态后重试。",
            ret=ret, retryable=False,
        )
    if is_busy:
        raise RiskControlError(
            f"触发风控（ret={ret} msg={msg}）。已停止继续派发请求。",
            ret=ret,
        )
    if ret in _AUTH_RETS:
        raise AuthError(
            f"无权访问该音频（ret={ret} msg={msg}）。"
            "可能需要登录或该内容在当前地区/账号下不可用。"
        )
    if ret == 3005:
        raise AuthError(
            f"无权访问该音频（ret={ret} msg={msg}）。"
            "可能需要登录或该内容在当前地区/账号下不可用。"
        )
    raise ApiError(
        f"未能获取播放信息（ret={ret} msg={msg}）。",
        ret=ret,
        retryable=True,
    )


class HttpSource:
    """纯 HTTP 音源：实现 `Source` 端口，配合 `SignProvider` + 登录 Cookie 走 baseInfo。"""

    def __init__(
        self,
        decoder: Decoder,
        sign_provider: SignProvider,
        chrome_path: str = "",
        profile_dir: str = "",
        cookies_cache_path: str = "",
        resolve_timeout: int = 40,
        chrome_headless: bool = True,
        risk_recorder: RiskEventRecorder | None = None,
        cookie_max_age: int = 1800,
        chrome_fallback=None,
        impersonate: str = "chrome146",
    ):
        """`impersonate` 是 curl-cffi 的可选传输配置，不建立授权，也不保证服务端接受。

        非空时要求安装 curl-cffi；调用方仍必须处理认证失败和服务端风控响应。
        """
        """`chrome_fallback`：可选的 `ChromeSource`，用于 `interactive_login` /
        `inspect_storage` 这两类**与音源后端无关**的命令——它们本来就和"如何获取
        播放地址"无关（登录只是把会话落到 Chrome Profile；inspect 只是列设备
        标识 key）。`xdl login` 走它把登录态写进 ~/.xdl/chrome-profile，
        `xdl refresh-cookies` 再从那个 Profile 提取 Cookie 给 HttpSource 用。
        """
        self._decoder = decoder
        self._sign = sign_provider
        self._chrome_path = chrome_path
        self._profile_dir = profile_dir
        self._cookies_cache_path = cookies_cache_path
        self._resolve_timeout = resolve_timeout
        self._chrome_headless = chrome_headless
        self._risk_recorder = risk_recorder
        self._cookie_max_age = cookie_max_age
        self._chrome_fallback = chrome_fallback
        self._impersonate = impersonate
        if impersonate and not _HAS_CURL_CFFI:
            raise ConfigError(
                "未安装 curl-cffi。请运行 `pip install curl-cffi` 后再使用 http 后端，"
                "或把 Settings.source_impersonate 置空改用 Python requests。"
            )
        # 运行态
        self._cookies: list[dict] = []
        self._cookie_header: str = ""
        self._authenticated: bool | None = None
        # 风控事件统计与会话标记
        self._session_id = str(uuid.uuid4())
        self._request_index = 0
        self._in_flight = 0

    # ---- Source 端口：会话生命周期 ----
    async def open(self) -> None:
        if self._cookies:
            return
        if not self._profile_dir or not os.path.isdir(self._profile_dir):
            raise ConfigError(
                f"未找到 Chrome Profile 目录: {self._profile_dir!r}。"
                "请先运行 `xdl login` 创建专用 Chrome Profile。"
            )
        self._sign.open()
        await self._load_cookies()

    async def close(self) -> None:
        try:
            self._sign.close()
        except Exception:
            pass

    async def _load_cookies(self) -> None:
        # 1) 先读缓存
        cached = (await asyncio.to_thread(
            load_cached_cookies, self._cookies_cache_path, self._cookie_max_age)
            if self._cookies_cache_path else None)
        if cached and is_login_cookie(cached):
            self._cookies = cached
        else:
            # 2) 缓存失效、缺失或只是匿名 Cookie：从 Profile 重读。
            self._cookies = await asyncio.to_thread(
                extract_cookies_from_profile,
                self._profile_dir,
                self._chrome_path,
                self._chrome_headless,
            )
            if is_login_cookie(self._cookies):
                await self._save_authenticated_cookies(self._cookies)
        if not self._cookies:
            raise AuthError(
                "专用 Chrome Profile 中未取到任何 Cookie。"
                "请重新 `xdl login` 确保登录态后重试。"
            )
        self._cookie_header = build_cookie_header(self._cookies)
        self._authenticated = is_login_cookie(self._cookies)
        if not self._authenticated:
            print("[warn] 当前 Cookie 中没有发现登录 token（1&_token），会员/已购内容将被拒。")

    async def refresh_cookies(self) -> None:
        """强制重新从 Chrome profile 读 Cookie（覆盖缓存）。"""
        cookies = await asyncio.to_thread(
            extract_cookies_from_profile,
            self._profile_dir,
            self._chrome_path,
            self._chrome_headless,
        )
        await self._save_authenticated_cookies(cookies)
        self._cookies = cookies
        self._cookie_header = build_cookie_header(cookies)
        self._authenticated = True

    def _build_base_info_url(self) -> str:
        """构造 baseInfo 端点 URL：在路径里嵌入当前毫秒时间戳。

        平台页面发的是 `/mobile-playpage/track/v3/baseInfo/{ms_ts}`，时间戳在路径
        而不在 query；服务端用它做签名时效校验的一部分。每次请求都重新生成。
        """
        import time
        return sign_conf.BASE_INFO_URL.format(ts=int(time.time() * 1000))

    def _http_get(self, url: str, params: dict, headers: dict):
        """发 baseInfo GET，并统一收敛所选 HTTP 客户端的响应。"""
        if self._impersonate and _HAS_CURL_CFFI:
            return cffi_requests.get(
                url, params=params, headers=headers,
                impersonate=self._impersonate,
                timeout=self._resolve_timeout,
            )
        # 回退：标准 requests 客户端。
        return requests.get(
            url, params=params, headers=headers,
            timeout=self._resolve_timeout,
        )

    # ---- Source 端口：单曲 ----
    async def get_track(self, track_id: str) -> Track:
        if not self._cookies:
            raise NetworkError("会话未打开，请先 await open()。")
        started = time.perf_counter()
        started_at = datetime.now(timezone.utc).isoformat()
        self._request_index += 1
        request_index = self._request_index
        self._in_flight += 1
        observed_in_flight = self._in_flight
        recorded = False
        try:
            try:
                sign_value = await asyncio.to_thread(self._sign.sign)
            except Exception as e:
                if not isinstance(e, SignError):
                    e = SignError(f"xm-sign 生成失败: {e}")
                self._record(track_id, started, e.category, None, str(e),
                             observed_in_flight, started_at, request_index)
                recorded = True
                raise e from None

            # 只补业务所需的 Accept / Referer / Origin / Cookie / xm-sign 字段。
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": platform.SOUND_URL.format(track_id=track_id),
                "Origin": platform.BASE,
                "Cookie": self._cookie_header,
                "xm-sign": sign_value,
            }
            if not self._impersonate:
                # 回退到 requests 时必填 UA（requests UA 是 python-requests/xxx，立刻被判罚）
                headers["User-Agent"] = platform.UA
            url = self._build_base_info_url()
            params = {
                "device": sign_conf.BASE_INFO_DEVICE,
                "trackId": str(track_id),
                "trackQualityLevel": str(sign_conf.BASE_INFO_QUALITY_LEVEL),
            }
            try:
                resp = await asyncio.to_thread(
                    self._http_get, url, params, headers,
                )
            except Exception as e:
                err = NetworkError(f"baseInfo 请求失败: {e}")
                self._record(track_id, started, err.category, None, str(err),
                             observed_in_flight, started_at, request_index)
                recorded = True
                raise err from e

            try:
                body = resp.json()
            except Exception as e:
                err = ApiError(
                    f"baseInfo 响应不是 JSON：{resp.text[:200]!r}",
                    ret=None, retryable=True,
                )
                self._record(track_id, started, err.category, None, str(err),
                             observed_in_flight, started_at, request_index)
                recorded = True
                raise err from e

            ret = body.get("ret")
            msg = body.get("msg")
            data = body.get("data") or {}
            # v3 baseInfo 把 trackInfo 直接挂在 body 上（不在 data 内），且单查时
            # 无 data 包装；两种结构都兼容。
            track_info = data.get("trackInfo") or body.get("trackInfo") or {}
            play_url_list = track_info.get("playUrlList") or []

            # v3 baseInfo 用 ret=0 表成功（与 getTracksList 的 ret=200 不同）。
            # 误把 ret=0 当失败会丢掉真实成功响应并触发不必要重试，最终撞风控。
            if ret not in (0, 200) or not play_url_list:
                is_busy = ret == 1001 or (ret == 3005 and "系统繁忙" in str(msg or ""))
                outcome = (
                    "risk_control" if is_busy and self._authenticated is not False
                    else "api_error" if is_busy  # 匿名 1001 算 api_error 而非风控
                    else "api_error"
                )
                self._record(track_id, started, outcome, ret, msg,
                             observed_in_flight, started_at, request_index)
                recorded = True
                _raise_for_ret(ret, msg, authenticated=self._authenticated)

            play_urls: list[PlayUrl] = []
            for item in play_url_list:
                enc = item.get("url")
                if not enc:
                    continue
                play_urls.append(PlayUrl(
                    type=item.get("type", ""),
                    url=self._decoder.decode(enc),
                    file_size=int(item.get("fileSize", 0) or 0),
                ))
            self._record(track_id, started, "success", None, None,
                         observed_in_flight, started_at, request_index)
            recorded = True
            return Track(
                track_id=str(track_id),
                title=track_info.get("title") or str(track_id),
                play_urls=play_urls,
                is_paid=bool(track_info.get("isPaid")),
                is_authorized=bool(track_info.get("isAuthorized", True)),
            )
        except (ApiError, AuthError, NetworkError, SignError, RiskControlError) as error:
            if not recorded:
                self._record(track_id, started, error.category,
                             getattr(error, "ret", None), str(error),
                             observed_in_flight, started_at, request_index)
            raise
        except Exception as error:
            if not recorded:
                self._record(track_id, started, "unexpected", None,
                             type(error).__name__, observed_in_flight,
                             started_at, request_index)
            raise
        finally:
            self._in_flight -= 1

    # ---- Source 端口：专辑清单（免签公开接口，纯 HTTP） ----
    async def get_album(self, album_id: str) -> Album:
        return await asyncio.to_thread(_fetch_album_list, str(album_id))

    # ---- 与音源后端无关的命令（委托给 ChromeSource 兜底） ----
    def interactive_login(self) -> str:
        """打开 Chrome 完成登录、保存到专用 Profile（共用 `xdl login` 流程）。

        登录态与音源后端无关：HttpSource 只是从这个 Profile 提取登录 Cookie
        —— `xdl login` 这一步仍走真实的 Chrome 浏览器（用户交互登录），画完
        `1&_token` 后由 `xdl refresh-cookies`（或登录时自动）把 Cookie 拷到
        ~/.xdl/cookies.json 供纯 HTTP 路径复用。
        """
        if self._chrome_fallback is None:
            raise ConfigError(
                "未配置 chrome_fallback；无法在纯 HTTP 后端下交互登录。"
                "请确认装配根注入了 ChromeSource（见 composition.build_facade）。")
        path = self._chrome_fallback.interactive_login()
        # 登录成功后同步把 Cookie 刷到缓存；没有 token 就视为失败，绝不覆盖旧缓存。
        try:
            cookies = extract_cookies_from_profile(
                self._profile_dir, self._chrome_path,
                headless=self._chrome_headless,
            )
        except Exception as e:
            raise AuthError(f"登录后导出 Cookie 失败: {e}") from e
        self._save_authenticated_cookies_sync(cookies)
        self._cookies = cookies
        self._cookie_header = build_cookie_header(cookies)
        self._authenticated = True
        return path

    async def _save_authenticated_cookies(self, cookies: list[dict]) -> None:
        self._require_login_cookie(cookies)
        if self._cookies_cache_path:
            await asyncio.to_thread(
                save_cookies, cookies, self._cookies_cache_path,
            )

    def _save_authenticated_cookies_sync(self, cookies: list[dict]) -> None:
        """仅在 token 存在时更新缓存，避免匿名结果覆盖有效登录态。"""
        self._require_login_cookie(cookies)
        if self._cookies_cache_path:
            save_cookies(cookies, self._cookies_cache_path)

    @staticmethod
    def _require_login_cookie(cookies: list[dict]) -> None:
        if not is_login_cookie(cookies):
            raise AuthError(
                "专用 Chrome Profile 中未发现登录 token（1&_token）；"
                "登录未完成或未持久化，未覆盖现有 Cookie 缓存。"
            )

    async def inspect_storage(self) -> dict:
        """诊断：列出 Profile 设备标识存储 key（不读 value），委托给 ChromeSource。"""
        if self._chrome_fallback is None:
            raise ConfigError(
                "未配置 chrome_fallback；无法在纯 HTTP 后端下做 Profile 诊断。")
        return await self._chrome_fallback.inspect_storage()

    # ---- 内部：风控观测 ----
    def _record(self, track_id: str, started: float, outcome: str,
                ret, msg, in_flight: int, started_at: str,
                request_index: int) -> None:
        if self._risk_recorder is None:
            return
        try:
            self._risk_recorder.record(
                track_id=str(track_id),
                elapsed_ms=round((time.perf_counter() - started) * 1000),
                outcome=outcome,
                ret=ret,
                msg=msg,
                in_flight=in_flight,
                session_id=self._session_id,
                request_index=request_index,
                started_at=started_at,
                authenticated=self._authenticated,
                device_fingerprint_reset=False,
            )
        except OSError:
            # 观测失败不影响真实平台响应或阻断下载
            pass
