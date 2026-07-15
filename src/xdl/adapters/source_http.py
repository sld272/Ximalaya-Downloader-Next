# -*- coding: utf-8 -*-
"""默认 HTTP 音源适配器。

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
                           load_cached_cookies, save_cookies, is_login_cookie,
                           strip_device_cookies)
from .sign.extractor import (identity_fingerprint,
                             refresh_device_identity_via_browser,
                             save_device_info, summarize_extract)
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
            "请先运行 `xdl login`，登录成功后直接重试。",
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
        experiment_rotate_device_on_risk: bool = False,
        experiment_browser_clear_state: bool = True,
        experiment_browser_fresh_profile: bool = False,
        experiment_persist_device_info: bool = True,
        experiment_strip_device_cookies: bool = True,
        experiment_max_rotations: int = 0,
        device_info_path: str = "",
    ):
        """初始化 HTTP 音源。

        `impersonate` 是 curl-cffi 的可选传输配置，不建立授权，也不保证服务端接受；
        非空时要求安装 curl-cffi。`chrome_fallback` 是可选的 `ChromeSource`，用于
        `interactive_login` /
        `inspect_storage` 这两类**与音源后端无关**的命令——它们本来就和"如何获取
        播放地址"无关（登录只是把会话落到 Chrome Profile；inspect 只是列设备
        标识 key）。`xdl login` 走它把登录态写进 ~/.xdl/chrome-profile，
        登录成功后会自动从该 Profile 导出 Cookie 给 HttpSource 使用。

        实验开关（默认关闭）：命中已识别风控时，通过真实浏览器重生设备指纹并
        重试当前曲。不保证服务端接受，也不是默认抗风控策略。
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
        self._experiment_rotate = bool(experiment_rotate_device_on_risk)
        self._experiment_browser_clear = bool(experiment_browser_clear_state)
        self._experiment_fresh_profile = bool(experiment_browser_fresh_profile)
        self._experiment_persist = bool(experiment_persist_device_info)
        self._experiment_strip_cookies = bool(experiment_strip_device_cookies)
        self._experiment_max_rotations = max(0, int(experiment_max_rotations))
        self._device_info_path = device_info_path or ""
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
        self._device_rotations = 0
        self._device_fingerprint_was_reset = False
        # 新指纹先只在当前 signer 中试用；首次受保护请求成功后才写入正式文件。
        self._pending_device_info: dict | None = None
        # 换身策略：换身后首次请求成功 → 允许再次换身；
        # 换身后首次请求仍是风控 → 本会话停用换身，避免无效连打。
        self._rotate_awaiting_success = False
        self._rotate_disabled = False
        self._rotate_lock = asyncio.Lock()

    # ---- Source 端口：会话生命周期 ----
    async def open(self) -> None:
        if not self._cookies:
            await self._load_cookies()
        # close() 后再次 open() 时必须重新打开 signer；不能只凭 Cookie 已加载就返回。
        self._sign.open()

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
            if not self._profile_dir or not os.path.isdir(self._profile_dir):
                raise ConfigError(
                    f"未找到 Chrome Profile 目录: {self._profile_dir!r}。"
                    "请先运行 `xdl login` 创建并保存登录态。"
                )
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
        # 风控换身：本曲最多换一次；换身后首次成功则本会话允许后续再换，
        # 换身后首次仍风控则停用本会话换身。默认关闭时循环只跑一轮。
        rotated_for_this_track = False
        while True:
            try:
                track = await self._get_track_once(track_id)
                if rotated_for_this_track:
                    await self._mark_post_rotate_success()
                return track
            except RiskControlError:
                if rotated_for_this_track:
                    self._disable_rotate_after_immediate_risk()
                    raise
                if not await self._maybe_rotate_after_risk():
                    raise
                rotated_for_this_track = True
                # 换身成功：用新 device_info / Cookie 重试当前曲目。

    async def _get_track_once(self, track_id: str) -> Track:
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

    async def _mark_post_rotate_success(self) -> None:
        """换身后的首次成功：证明新指纹可用，允许本会话后续再次换身。"""
        if not self._rotate_awaiting_success:
            return
        pending_info = self._pending_device_info
        self._pending_device_info = None
        if pending_info is not None and self._device_info_path:
            try:
                await asyncio.to_thread(
                    save_device_info, pending_info, self._device_info_path,
                )
            except OSError as e:
                print(f"[warn] 已验证的新设备指纹写盘失败: {e}")
        self._rotate_awaiting_success = False
        print(
            f"[experiment] 换身后请求成功（累计换身 {self._device_rotations} 次），"
            "后续再遇风控仍可换身"
        )

    def _disable_rotate_after_immediate_risk(self) -> None:
        """换身后首次请求仍风控：本会话停用换身，避免无效连打。"""
        self._pending_device_info = None
        self._rotate_awaiting_success = False
        if self._rotate_disabled:
            return
        self._rotate_disabled = True
        print("[experiment] 换身后首次请求仍触发风控，停止本会话继续换身")

    async def _maybe_rotate_after_risk(self) -> bool:
        """风控后是否执行实验性换身。成功返回 True 并允许重试当前曲。

        策略：
        - 换身后、尚未出现成功请求前，不再叠加换身（由 get_track 本地标记判定
          首次探针失败并停用）；
        - 换身后曾成功 → 允许再次换身；
        - `experiment_max_rotations > 0` 时额外施加硬上限；`0` 表示不限次数。
        """
        async with self._rotate_lock:
            if not self._experiment_rotate:
                return False
            if self._rotate_disabled:
                return False
            if self._rotate_awaiting_success:
                # 上一次换身的探针尚未出结果，不叠加换身。
                return False
            if (self._experiment_max_rotations > 0
                    and self._device_rotations >= self._experiment_max_rotations):
                print(
                    f"[experiment] 已达换身硬上限 "
                    f"{self._experiment_max_rotations}，停止换身"
                )
                return False
            try:
                await self.rotate_device_identity()
            except Exception as e:
                print(f"[warn] 实验换身失败，保持熔断: {e}")
                return False
            self._rotate_awaiting_success = True
            return True

    async def rotate_device_identity(self) -> str:
        """通过真实浏览器重生设备指纹，并可选更新 Cookie。

        打开 Chrome，清设备 Cookie/storage，让 du_web_sdk 重生后再采集
        device_info。需要 SignProvider 实现 `reload(device_info)`。
        不保证服务端接受新身份。
        """
        reload_fn = getattr(self._sign, "reload", None)
        if not callable(reload_fn):
            raise ConfigError(
                "当前 SignProvider 不支持 reload(device_info)，无法做指纹换身实验。"
            )
        if not self._experiment_fresh_profile and not self._profile_dir:
            raise ConfigError(
                "换身需要 chrome_profile_dir，或开启 experiment_browser_fresh_profile。"
            )

        result = await asyncio.to_thread(
            refresh_device_identity_via_browser,
            profile_dir=self._profile_dir,
            chrome_path=self._chrome_path,
            headless=self._chrome_headless,
            clear_device_state=self._experiment_browser_clear,
            fresh_profile=self._experiment_fresh_profile,
        )
        print(f"[experiment] 浏览器提取：{summarize_extract(result)}")
        cookie_note = self._apply_rotated_cookies(result)

        new_info = result.device_info
        await asyncio.to_thread(reload_fn, new_info)
        self._pending_device_info = (
            new_info if self._experiment_persist and self._device_info_path else None
        )

        self._device_rotations += 1
        self._device_fingerprint_was_reset = True
        fp = identity_fingerprint(new_info)
        limit = (str(self._experiment_max_rotations)
                 if self._experiment_max_rotations > 0 else "∞")
        print(
            f"[experiment] 已换设备指纹 identity={fp} "
            f"(第 {self._device_rotations}/{limit} 次；{cookie_note})"
        )
        return fp

    def _apply_rotated_cookies(self, result) -> str:
        """把浏览器导出的 Cookie 应用到当前 HTTP 会话，返回日志摘要。"""
        cookie_note = "cookies=unchanged"
        if result.cookies:
            cookies = result.cookies
            if self._experiment_strip_cookies:
                cookies = strip_device_cookies(cookies)
            if is_login_cookie(cookies) or not self._cookies:
                self._cookies = cookies
                self._cookie_header = build_cookie_header(cookies)
                self._authenticated = is_login_cookie(cookies)
                cookie_note = (
                    f"cookies=browser_export login={self._authenticated} "
                    f"cleared={','.join(result.cleared_cookie_names[:8]) or '-'}"
                )
                if is_login_cookie(cookies) and self._cookies_cache_path:
                    try:
                        save_cookies(cookies, self._cookies_cache_path)
                    except OSError:
                        pass
            else:
                if self._experiment_strip_cookies and self._cookies:
                    self._cookies = strip_device_cookies(self._cookies)
                    self._cookie_header = build_cookie_header(self._cookies)
                    self._authenticated = is_login_cookie(self._cookies)
                cookie_note = "cookies=kept_login_stripped_device(export_lost_token)"
        elif self._experiment_strip_cookies and self._cookies:
            self._cookies = strip_device_cookies(self._cookies)
            self._cookie_header = build_cookie_header(self._cookies)
            self._authenticated = is_login_cookie(self._cookies)
            cookie_note = "cookies=stripped_device_only"
        return cookie_note

    # ---- Source 端口：专辑清单（免签公开接口，纯 HTTP） ----
    async def get_album(self, album_id: str) -> Album:
        return await asyncio.to_thread(_fetch_album_list, str(album_id))

    # ---- 与音源后端无关的命令（委托给 ChromeSource 兜底） ----
    def interactive_login(self) -> str:
        """打开 Chrome 完成登录、保存到专用 Profile（共用 `xdl login` 流程）。

        登录态与音源后端无关：HttpSource 只是从这个 Profile 提取登录 Cookie
        —— `xdl login` 这一步仍走真实的 Chrome 浏览器（用户交互登录），取得
        `1&_token` 后自动把 Cookie 拷到
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
                device_fingerprint_reset=self._device_fingerprint_was_reset,
            )
        except OSError:
            # 观测失败不影响真实平台响应或阻断下载
            pass
