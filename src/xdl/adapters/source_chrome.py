# -*- coding: utf-8 -*-
"""在线音源适配器（实现 Source 端口，见 docs/architecture.md §7.1/§7.2）。

单曲解析走「让页面自己签名」：加载已登录的 /sound 页，页面内 du_web_sdk 生成
xm-sign 并发出 baseInfo 请求，适配器只读监听网络响应并提取目标 trackId 的结果。
不注入或改写页面的 XHR/fetch，也不自动点击页面元素。

平台 du_web_sdk 对 baseInfo 有自动化环境风控。这里自己启动真实 Chrome，再用
Playwright `connect_over_cdp` 接管，登录态持久化在专用 Profile。但 2026-07-11 复测
显示，有头模式也会弹验证码；CDP、新 Profile、每曲新页面和页面请求扇出仍是可疑变量，
不能再宣称“真实 Chrome + CDP”天然通过风控。

并发：采用 async Playwright，每次解析在共享上下文里开一个独立 page。2026-07-11
复测中 K=4 的 8 次解析有 7 次返回 3005，因此生产默认串行；并发能力仅保留给
受控测试，不能当作安全阈值。

专辑清单不经浏览器：走免签的「非 v1」getTracksList 接口纯 HTTP 翻页。
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import socket
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from ..config import platform
from ..domain import Track, PlayUrl, Album, AlbumTrack
from ..errors import (XdlError, ApiError, AuthError, NetworkError, ConfigError,
                      RiskControlError)
from ..ports import Decoder
from ..risk import RiskEventRecorder

_AUTH_RETS = {927}   # 无权访问/地区限制等鉴权类
_LIST_TIMEOUT = 30
_MAX_PAGES = 2000


# 设备指纹 Cookie 名前缀。清除这些、保留登录 Cookie（1&_token / 1&remember_me /
# web_login 等），等同"在新设备登录同一账号"：下次访问页面时平台 SDK 会为此该 Profile
# 重新生成 _xmLog/wfp，Hm_lvt_* 首次访问时间戳也一并归零，旧设备上累积的验证码惩罚态
# 不带入新设备。已通过用户日常浏览器对照确认风控跟设备而非跟账号走。见
# docs/risk-control-observations.md 的设备 Cookie 差分。
_DEVICE_COOKIE_PREFIXES = ("_xmLog", "wfp", "Hm_lvt_", "Hm_lpvt_")


def _is_device_fingerprint_cookie(name) -> bool:
    return str(name or "").startswith(_DEVICE_COOKIE_PREFIXES)


def _partition_device_cookies(cookies):
    """把 Cookie 列表分成（待删除的设备指纹名称, 保留的 Cookie 列表）。"""
    removed: list[str] = []
    kept: list[dict] = []
    for c in cookies:
        if _is_device_fingerprint_cookie(c.get("name")):
            removed.append(str(c.get("name")))
        else:
            kept.append(c)
    return removed, kept


def _port_alive(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _has_login_cookie(cookies: list[dict]) -> bool:
    """只判断登录 token 是否存在；不读取、持久化或输出 token 内容。"""
    return any(
        str(cookie.get("name") or "").endswith("&_token")
        and bool(cookie.get("value"))
        for cookie in cookies
    )


def _has_persisted_login_cookie(profile_dir: str) -> bool:
    """关闭 Chrome 后，只凭 Cookie DB 元数据确认登录 token 已落盘。

    这里只查询 Cookie 名和密文长度，不读取或解密 token 值。登录流程先在 CDP 上
    确认 Cookie 存在，再在 Chrome 正常退出后调用本函数，避免把只存在于内存中的
    Cookie 当作已保存的登录态。
    """
    candidates = (
        Path(profile_dir) / "Default" / "Network" / "Cookies",
        Path(profile_dir) / "Default" / "Cookies",
    )
    for db_path in candidates:
        if not db_path.is_file():
            continue
        try:
            conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT 1 FROM cookies "
                    "WHERE name = ? "
                    "AND (length(encrypted_value) > 0 OR length(value) > 0) "
                    "LIMIT 1",
                    ("1&_token",),
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                return True
        except sqlite3.Error:
            # 旧 Chrome 的表结构、损坏 Profile 或仍被占用的 DB 均不应被误判为已保存。
            continue
    return False


# 仅认「主动发起的图形验证挑战」信号（GeeTest v4 的 load / 惩罚接口）。刻意不含
# fe-captcha 这类可能被页面惰性预载、并不代表真的弹了验证码的模块，避免把偶发超时
# 误判成风控而错误熔断整批。
_CAPTCHA_HINTS = ("gcaptcha4.geetest.com", "api.geetest.com",
                  "/punish", "nvcresource")


def _is_captcha_url(url: str) -> bool:
    """页面是否真正发起了图形验证码挑战（风控惩罚态的强信号）。"""
    low = str(url or "").lower()
    return any(hint in low for hint in _CAPTCHA_HINTS)


def _needs_captcha_fallback(last_err) -> bool:
    """本次失败是否为‘弹了图形验证码’——只有这种才值得切有头让用户手动过。"""
    return isinstance(last_err, dict) and bool(last_err.get("captcha"))


# 自动播放被拦时的兜底：点击页面上的“播放”类元素以触发 baseInfo。仅作为
# Playwright 真实点击找不到明确控件时的回退（真实点击更接近真人交互）。
_TRIGGER_PLAY_JS = r"""
() => {
  for (const el of document.querySelectorAll('button,div[role="button"],span,i,svg,a')) {
    const cls = el.className && el.className.baseVal !== undefined
      ? el.className.baseVal : (el.className || '');
    const hint = (el.getAttribute('aria-label') || '')
      + (el.getAttribute('title') || '') + cls;
    if (/play|播放/i.test(hint)) { try { el.click(); } catch (e) {} }
  }
}
"""


# 诊断：列出页面侧设备标识存储的 key 名（刻意不读 value），用于判断"清 Cookie"是否
# 已覆盖所有承载设备 ID / browser_id 的存储面。只返回 key 名 + value 长度，避免泄露。
_INSPECT_STORAGE_JS = r"""
async () => {
  const out = { localStorage: [], sessionStorage: [], indexedDB: [],
                cookieNames: [], origin: location.origin };
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      out.localStorage.push({key: k, len: (localStorage.getItem(k) || '').length});
    }
  } catch (e) { out.localStorageError = String(e); }
  try {
    for (let i = 0; i < sessionStorage.length; i++) {
      const k = sessionStorage.key(i);
      out.sessionStorage.push({key: k, len: (sessionStorage.getItem(k) || '').length});
    }
  } catch (e) { out.sessionStorageError = String(e); }
  try {
    if (indexedDB.databases) {
      const dbs = await indexedDB.databases();
      for (const db of dbs) out.indexedDB.push({name: db.name, version: db.version});
    } else {
      out.indexedDBNote = 'indexedDB.databases() 不可用（旧版 Chrome）';
    }
  } catch (e) { out.indexedDBError = String(e); }
  try {
    out.cookieNames = document.cookie.split(';')
      .map(c => c.trim().split('=')[0]).filter(Boolean);
  } catch (e) { out.cookieError = String(e); }
  return out;
}
"""

# 用于诊断的目标示例曲目（公开存在）
_INSPECT_TRACK_ID = "852566950"


# 清空页面 origin 下的 localStorage / sessionStorage / IndexedDB。专用 Profile 只用于
# 喜马拉雅，里面没有用户原生内容，可放心清空。inspector 实测显示 localStorage 里
# 承载设备身份/反垃圾指纹的 key 有 _antispam_ / crystal / cid / assva5 ... / cmci9xdq
# / vmce9xdq 等，IndexedDB 里有 "treasure" 埋点 SDK 库——只清 Cookie 远远不够，
# 服务端会通过这些 storage 内的设备指纹把"换 Cookie 后的新身"再次关联到被惩罚身份。
_CLEAR_STORAGE_JS = r"""
async () => {
  const result = { localStorageCleared: 0, sessionStorageCleared: 0,
                   indexedDB: [], indexedDBError: null };
  try {
    result.localStorageCleared = localStorage.length;
    localStorage.clear();
  } catch (e) { result.localStorageError = String(e); }
  try {
    result.sessionStorageCleared = sessionStorage.length;
    sessionStorage.clear();
  } catch (e) { result.sessionStorageError = String(e); }
  try {
    if (indexedDB.databases) {
      const dbs = await indexedDB.databases();
      for (const db of dbs) {
        if (!db.name) continue;
        await new Promise((resolve) => {
          let settled = false;
          const done = () => { if (!settled) { settled = true;
                                              clearTimeout(t); resolve(); } };
          const t = setTimeout(done, 3000);
          const req = indexedDB.deleteDatabase(db.name);
          req.onsuccess = done; req.onerror = done; req.onblocked = done;
        });
        result.indexedDB.push(db.name);
      }
    } else {
      result.indexedDBError = "indexedDB.databases() 不可用";
    }
  } catch (e) { result.indexedDBError = String(e); }
  return result;
}
"""


def _cookie_summary(cookies):
    """列 Cookie 名 + 域 + 标记，不读 value。"""
    return [
        {"name": c.get("name"), "domain": c.get("domain"),
         "httpOnly": c.get("httpOnly"), "secure": c.get("secure")}
        for c in cookies
    ]


def _log_reset_result(cookie_names: list[str], storage_report) -> None:
    """格式化输出本次设备指纹重置的清理明细（不包含 Cookie/指纹值）。"""
    parts: list[str] = []
    if cookie_names:
        parts.append("Cookie: " + ", ".join(cookie_names))
    if isinstance(storage_report, dict):
        ls = int(storage_report.get("localStorageCleared") or 0)
        ss = int(storage_report.get("sessionStorageCleared") or 0)
        parts.append(f"localStorage 清 {ls} 项，sessionStorage 清 {ss} 项")
        idb = storage_report.get("indexedDB") or []
        if idb:
            parts.append("IndexedDB: " + ", ".join(str(x) for x in idb))
    msg = "已重置设备指纹（保留登录态），下次访问将生成新设备标识 — " + "；".join(parts)
    print(msg)


def _parse_base_info_payload(url: str, body: dict, target_track_id: str):
    """从网络响应中提取目标曲目的播放信息或错误，不修改页面 JavaScript。"""
    if "baseInfo" not in url or not isinstance(body, dict):
        return None
    query = parse_qs(urlparse(url).query)
    request_track_id = str((query.get("trackId") or [""])[0])
    if request_track_id != str(target_track_id):
        return None
    track_info = body.get("trackInfo") or (body.get("data") or {}).get("trackInfo")
    if track_info and track_info.get("playUrlList"):
        return track_info, None
    return None, {"ret": body.get("ret"), "msg": body.get("msg")}


class ChromeSource:
    """经 CDP 接管真实 Chrome 解析单曲（async，可并发）；HTTP 取专辑清单。"""

    def __init__(self, decoder: Decoder, chrome_path: str, profile_dir: str,
                 port: int = 9222, resolve_timeout: int = 40, headless: bool = True,
                 risk_recorder: RiskEventRecorder | None = None,
                 risk_fallback_headful: bool = True,
                 reset_device_fingerprint: bool = True):
        self._decoder = decoder
        self._chrome_path = chrome_path
        self._profile_dir = profile_dir
        self._port = port
        self._timeout = resolve_timeout
        self._headless = headless
        self._risk_recorder = risk_recorder
        # 遇验证码风控时是否自动切有头让用户手动通过一次。
        self._risk_fallback_headful = risk_fallback_headful
        # 是否在会话启动/登录后清除设备指纹 Cookie（_xmLog/wfp/Hm_lvt_*），保留登录态。
        self._reset_device_fingerprint = bool(reset_device_fingerprint)
        # 本会话是否已成功重置过设备指纹；用于风控事件上报与避免重复清空。
        self._device_fingerprint_was_reset = False
        self._escalated = False
        # 一次有头等待仍没等到用户过验证码后置位，避免后续每曲都白等一个长超时。
        self._recovery_failed = False
        self._in_flight = 0
        self._risk_session_id = str(uuid.uuid4())
        self._request_index = 0
        self._authenticated: bool | None = None
        # 无头会话下用来抹掉 "HeadlessChrome" UA 指纹；"" 表示已探测且无需覆盖。
        self._ua_override: str | None = None
        self._proc = None
        self._pw = None
        self._browser = None
        self._ctx = None

    # ---- 会话生命周期（Source 端口） ----
    async def open(self) -> None:
        if self._ctx is not None:
            return
        self._require_chrome()
        os.makedirs(self._profile_dir, exist_ok=True)
        if not _port_alive(self._port):
            self._launch_chrome(headless=self._headless)
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self._port}")
        except Exception as e:
            await self.close()
            raise NetworkError(f"接管 Chrome 失败（端口 {self._port}）: {e}") from e
        self._ctx = (self._browser.contexts[0] if self._browser.contexts
                     else await self._browser.new_context())
        try:
            self._authenticated = _has_login_cookie(
                await self._ctx.cookies(platform.BASE)
            )
        except Exception:
            self._authenticated = None
        # 接管成功后重置设备指纹 Cookie（保留登录态），让下次访问生成新设备 ID，
        # 摆脱旧设备上的验证码惩罚态。详见 _reset_device_cookies 的说明。
        await self._reset_device_cookies()

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = self._pw = self._browser = self._ctx = None

    # ---- 设备指纹重置 ----
    @staticmethod
    async def _delete_device_cookies_via_cdp_async(session, cookies):
        """通过 CDP `Network.deleteCookie` 异步删除指定 cookies，返回被删的名称列表。"""
        removed = []
        for c in cookies:
            if _is_device_fingerprint_cookie(c.get("name")):
                try:
                    await session.send("Network.deleteCookie", {
                        "name": c.get("name", ""),
                        "domain": c.get("domain", ""),
                        "path": c.get("path", "/"),
                    })
                    removed.append(str(c.get("name")))
                except Exception:
                    pass
        return sorted(set(removed))

    @classmethod
    def _delete_device_cookies_via_cdp(cls, session, cookies):
        """同步版本：用 CDP `Network.deleteCookie` 逐个删设备指纹 Cookie。

        `Network.deleteCookie` 直接操作 Chrome 的 Cookie 数据库中对应行，不影响其他
        Cookie，登录态始终留在磁盘上。原先用 `clear_cookies()` 会有以下问题：删除落盘
        后重加只在内存，`browser.close()` 来不及刷盘导致 `1&_token` 丢失。
        """
        removed = []
        for c in cookies:
            if _is_device_fingerprint_cookie(c.get("name")):
                try:
                    session.send("Network.deleteCookie", {
                        "name": c.get("name", ""),
                        "domain": c.get("domain", ""),
                        "path": c.get("path", "/"),
                    })
                    removed.append(str(c.get("name")))
                except Exception:
                    pass
        return sorted(set(removed))

    async def _reset_device_cookies(self) -> None:
        """清除专用 Profile 中的设备指纹（Cookie + storage），保留登录态。

        等同"在新设备登录同一账号"：用户日常浏览器同账号无风控已证明喜马拉雅的
        设备风控跟设备标识走、不跟账号走。`xdl inspect` 实测显示设备身份在多个
        存储面同时落地：
          - Cookie:   _xmLog / wfp / Hm_lvt_*
          - localStorage:  _antispam_ / crystal / cid / assva5... / cmci9xdq / vmce9xdq
          - IndexedDB:     treasure（喜马拉雅埋点 SDK）
        只清 Cookie 不够——localStorage 里旧设备指纹会让"换 Cookie 后的新身"在几次
        请求后被服务端再次关联到被惩罚身份。本方法在清 Cookie 基础上，额外开一次页面
        访问首页、清空 origin 下 localStorage/sessionStorage/IndexedDB，让下次访问
        时 SDK 重新生成整套新设备标识。本会话只重置一次；失败不阻断正常解析。
        """
        if (not self._reset_device_fingerprint
                or self._device_fingerprint_was_reset
                or self._ctx is None):
            return
        cookie_names: list[str] = []
        page = None
        try:
            page = await self._ctx.new_page()
            client = await page.context.new_cdp_session(page)
            await client.send("Network.enable")
            cookies = await self._ctx.cookies()
            cookie_names = await self._delete_device_cookies_via_cdp_async(client, cookies)
        except Exception as e:
            import os
            if os.environ.get("XDL_DEBUG_RESET"):
                raise
            print(f"[warn] 设备 Cookie 重置失败: {e}")
        storage_report = None
        try:
            if page is not None:
                await page.goto(platform.HOME_URL, wait_until="domcontentloaded")
                storage_report = await page.evaluate(_CLEAR_STORAGE_JS)
        except Exception as e:
            print(f"[warn] 设备 Storage 重置失败: {e}")
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
        self._device_fingerprint_was_reset = True
        _log_reset_result(cookie_names, storage_report)

    def _reset_device_cookies_sync(self, context) -> None:
        """同步版（供 interactive_login 用）。语义与 _reset_device_cookies 一致：
        清设备 Cookie + 清空页面 origin 下的 storage，保留登录态。
        """
        if (not self._reset_device_fingerprint
                or self._device_fingerprint_was_reset
                or context is None):
            return
        cookie_names: list[str] = []
        page = None
        try:
            page = context.new_page()
            client = page.context.new_cdp_session(page)
            client.send("Network.enable")
            cookies = context.cookies()
            cookie_names = self._delete_device_cookies_via_cdp(client, cookies)
        except Exception as e:
            print(f"[warn] 设备 Cookie 重置失败: {e}")
        storage_report = None
        try:
            if page is not None:
                page.goto(platform.HOME_URL, wait_until="domcontentloaded")
                storage_report = page.evaluate(_CLEAR_STORAGE_JS)
        except Exception as e:
            print(f"[warn] 设备 Storage 重置失败: {e}")
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
        self._device_fingerprint_was_reset = True
        _log_reset_result(cookie_names, storage_report)

    async def inspect_storage(self) -> dict:
        """诊断：列出当前 Profile 下的设备标识存储 key 名（不读 value）。

        目的：验证"只清>(_xmLog/wfp/Hm_lvt_*) Cookie"是否覆盖了喜马拉雅设备 ID 的
        所有载体。如果 localStorage / IndexedDB 里也存在 device_id 类条目，则 Cookie
        清除不充分，需要扩展到 storage。刻意保持只读：不触发 _reset_device_cookies、
        不下载、不解码。返回结构只含 key 名 + value 长度，便于离线判断。
        """
        self._require_chrome()
        os.makedirs(self._profile_dir, exist_ok=True)
        launched_here = False
        if not _port_alive(self._port):
            self._launch_chrome(headless=True)
            launched_here = True
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        page = None
        browser = None
        try:
            browser = await pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self._port}")
            ctx = (browser.contexts[0] if browser.contexts
                   else await browser.new_context())
            cookies_before = await ctx.cookies()
            page = await ctx.new_page()
            report = {
                "cookies_before": _cookie_summary(cookies_before),
                "device_cookies_present": sorted({
                    c.get("name") for c in cookies_before
                    if _is_device_fingerprint_cookie(c.get("name"))
                }),
            }
            await page.goto(platform.HOME_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            report["home"] = await page.evaluate(_INSPECT_STORAGE_JS)
            await page.goto(platform.SOUND_URL.format(
                track_id=_INSPECT_TRACK_ID), wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            report["sound"] = await page.evaluate(_INSPECT_STORAGE_JS)
            cookies_after = await ctx.cookies()
            report["cookies_after"] = _cookie_summary(cookies_after)
            report["cookie_names_added_by_sound_visit"] = sorted(
                {c.get("name") for c in cookies_after}
                - {c.get("name") for c in cookies_before}
            )
            return report
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            try:
                if browser is not None:
                    await browser.close()
            except Exception:
                pass
            try:
                await pw.stop()
            except Exception:
                pass
            if launched_here and self._proc is not None:
                self._terminate_process(self._proc)
                self._proc = None

    # ---- Source 端口：单曲（每次开独立 page，支持并发） ----
    async def get_track(self, track_id: str) -> Track:
        if self._ctx is None:
            raise NetworkError("会话未打开，请先 await open()。")
        started = time.perf_counter()
        started_at = datetime.now(timezone.utc).isoformat()
        self._request_index += 1
        request_index = self._request_index
        self._in_flight += 1
        observed_in_flight = self._in_flight
        page = None
        recorded = False
        try:
            page = await self._ctx.new_page()
            node, last_err = await self._capture_base_info(page, track_id)
            # 撞到图形验证码：切/用有头弹窗，让用户手动过一次验证码后在有头会话里恢复
            # （给足时间，不需要敲回车，轮询到成功的 baseInfo 即恢复）。上一次有头等待
            # 仍没等到用户过验证码时不再重复白等。
            if (not node and _needs_captcha_fallback(last_err)
                    and self._risk_fallback_headful and not self._recovery_failed):
                page, node, last_err = await self._recover_via_headful(
                    page, track_id)
            if not node:
                ret = last_err.get("ret") if isinstance(last_err, dict) else None
                msg = last_err.get("msg") if isinstance(last_err, dict) else None
                captcha = bool(last_err.get("captcha")) if isinstance(last_err, dict) else False
                outcome = ("risk_control" if captcha or ret == 1001 or
                           (ret == 3005 and "系统繁忙" in str(msg or ""))
                           else "api_error")
                self._record_risk_event(track_id, started, outcome, ret, msg,
                                        observed_in_flight, started_at,
                                        request_index)
                recorded = True
                self._raise_for(last_err)

            play_urls = []
            for item in node.get("playUrlList") or []:
                enc = item.get("url")
                if not enc:
                    continue
                play_urls.append(PlayUrl(
                    type=item.get("type", ""),
                    url=self._decoder.decode(enc),
                    file_size=item.get("fileSize", 0) or 0,
                ))
            self._record_risk_event(track_id, started, "success", None, None,
                                    observed_in_flight, started_at, request_index)
            recorded = True
            return Track(
                track_id=str(track_id),
                title=node.get("title") or str(track_id),
                play_urls=play_urls,
                is_paid=bool(node.get("isPaid")),
                is_authorized=bool(node.get("isAuthorized", True)),
            )
        except XdlError as error:
            if not recorded:
                self._record_risk_event(
                    track_id, started, error.category,
                    getattr(error, "ret", None), str(error), observed_in_flight,
                    started_at, request_index,
                )
            raise
        except Exception as error:
            if not recorded:
                self._record_risk_event(track_id, started, "unexpected", None,
                                        type(error).__name__, observed_in_flight,
                                        started_at, request_index)
            raise
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            self._in_flight -= 1

    # ---- Source 端口：专辑清单（纯 HTTP，放线程池不挡事件循环） ----
    async def get_album(self, album_id: str) -> Album:
        return await asyncio.to_thread(self._fetch_album, str(album_id))

    def _fetch_album(self, album_id: str) -> Album:
        headers = {"User-Agent": platform.UA,
                   "Referer": platform.ALBUM_URL.format(album_id=album_id)}
        tracks: list[AlbumTrack] = []
        title: str | None = None
        total = 0
        for page_num in range(1, _MAX_PAGES + 1):
            try:
                resp = requests.get(
                    platform.TRACKS_LIST_URL,
                    params={"albumId": album_id, "pageNum": page_num, "sort": 0},
                    headers=headers, timeout=_LIST_TIMEOUT)
                resp.raise_for_status()
                body = resp.json()
            except requests.RequestException as e:
                raise NetworkError(f"获取专辑曲目清单失败: {e}") from e
            if body.get("ret") != 200 or not body.get("data"):
                raise ApiError(f"获取专辑曲目清单失败（ret={body.get('ret')} "
                               f"msg={body.get('msg')}）。", ret=body.get("ret"))
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

    # ---- 适配器特有：交互式登录（同步，一次性） ----
    def interactive_login(self) -> str:
        self._require_chrome()
        os.makedirs(self._profile_dir, exist_ok=True)
        if _port_alive(self._port):
            raise NetworkError(
                f"Chrome 调试端口 {self._port} 已被占用，请先关闭占用该端口的浏览器。"
            )
        print("即将打开 Chrome，请在其中完成登录（扫码或账号密码）。")
        args = [self._chrome_path,
                f"--remote-debugging-port={self._port}",
                f"--user-data-dir={self._profile_dir}",
                *platform.CHROME_LAUNCH_ARGS, platform.HOME_URL]
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        verified = False
        closed_cleanly = False
        try:
            for _ in range(75):
                if _port_alive(self._port):
                    break
                time.sleep(0.2)
            else:
                raise NetworkError(f"Chrome 调试端口 {self._port} 未就绪（启动超时）。")

            while not verified:
                input(">>> 登录完成后回到这里按回车，程序将验证登录状态: ")
                verified = self._verify_interactive_login()
                if not verified:
                    print("未检测到专用 Chrome 的登录状态，请在该窗口继续登录后重试。")
        finally:
            # 验证成功时 _verify_interactive_login 已通过 CDP 正常关闭浏览器，
            # 只等待其退出，异常/中断时才使用终止作为兜底。
            if verified:
                try:
                    proc.wait(timeout=10)
                    closed_cleanly = True
                except Exception:
                    self._terminate_process(proc)
            else:
                self._terminate_process(proc)
        if not closed_cleanly:
            raise NetworkError("Chrome 未能正常退出，无法确认登录态是否已持久化。")
        if not _has_persisted_login_cookie(self._profile_dir):
            raise AuthError(
                "登录 token 未持久化到专用 Chrome Profile；请重新登录后再试。"
            )
        return self._profile_dir

    def _verify_interactive_login(self) -> bool:
        """连接专用 Chrome 验证登录；成功后正常关闭浏览器以确保 Profile 刷盘。"""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self._port}")
            contexts = browser.contexts
            if not contexts:
                return False
            context = contexts[0]
            authenticated = _has_login_cookie(context.cookies(platform.BASE))
            if authenticated:
                browser.close()
            return authenticated

    @staticmethod
    def _terminate_process(proc) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ---- 内部 ----
    def _require_chrome(self) -> None:
        if not self._chrome_path or not os.path.exists(self._chrome_path):
            raise ConfigError(
                f"未找到 Chrome 可执行文件: {self._chrome_path!r}。"
                "请安装 Google Chrome，或在配置中指定 chrome_path。")

    def _launch_chrome(self, headless: bool) -> None:
        args = [self._chrome_path,
                f"--remote-debugging-port={self._port}",
                f"--user-data-dir={self._profile_dir}",
                *platform.CHROME_LAUNCH_ARGS]
        if headless:
            args.append("--headless=new")
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        for _ in range(75):
            if _port_alive(self._port):
                return
            time.sleep(0.2)
        raise NetworkError(f"Chrome 调试端口 {self._port} 未就绪（启动超时）。")

    async def _capture_base_info(self, page, track_id: str, timeout: float | None = None):
        wait_budget = self._timeout if timeout is None else timeout
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        tasks: set[asyncio.Task] = set()
        captcha_seen = {"hit": False}

        async def inspect_response(response) -> None:
            if "baseInfo" not in response.url:
                return
            try:
                body = await response.json()
            except Exception:
                return
            result = _parse_base_info_payload(response.url, body, str(track_id))
            if result is not None and queue.empty():
                queue.put_nowait(result)

        def on_response(response) -> None:
            # 实测：风控触发后页面会加载 GeeTest v4 图形验证码（gcaptcha4.geetest.com
            # 等），且目标 baseInfo 干脆不再发出。捕获该信号，好把纯超时和验证码风控区分开。
            if _is_captcha_url(response.url):
                captcha_seen["hit"] = True
            task = asyncio.create_task(inspect_response(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.on("response", on_response)
        try:
            await self._apply_ua_override(page)
            await page.goto(platform.SOUND_URL.format(track_id=track_id),
                            wait_until="domcontentloaded")
            # 先给页面一段自动发出 baseInfo 的时间（登录/预热态通常会自动发），但至多
            # 用掉一半预算，保证触发播放后仍有时间等目标响应回来。
            grace = min(4.0, wait_budget / 2)
            try:
                return await asyncio.wait_for(queue.get(), timeout=grace)
            except asyncio.TimeoutError:
                pass
            # 没有自动发：主动触发播放来让页面自己签名并发 baseInfo，避免用户手点。
            # 若已弹出验证码则跳过（点播放无意义，且不去碰验证码控件）。
            if not captcha_seen["hit"]:
                await self._trigger_play(page)
            remaining = max(0.0, wait_budget - grace)
            try:
                return await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                # 目标 baseInfo 在超时内没有作为 JSON 出现：要么被验证码拦截、要么
                # 页面根本没自动发。回一个带诊断标记的错误，避免上层看到无信息的
                # ret=None/msg=None。
                return None, {"ret": None, "msg": None, "timeout": True,
                              "captcha": captcha_seen["hit"]}
        finally:
            page.remove_listener("response", on_response)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _apply_ua_override(self, page) -> None:
        """无头会话下把 navigator.userAgent 与请求头里的 HeadlessChrome 抹成 Chrome。

        用页面实际 UA 派生，主版本号与真实浏览器一致，避免 UA 与 client-hints 错位。
        有头（本就是正常 Chrome）时不需要，探测一次后置空跳过。
        """
        if self._ua_override == "":
            return
        try:
            if self._ua_override is None:
                ua = await page.evaluate("navigator.userAgent")
                self._ua_override = (ua.replace("HeadlessChrome", "Chrome")
                                     if "HeadlessChrome" in (ua or "") else "")
            if not self._ua_override:
                return
            match = re.search(r"Chrome/(\d+)", self._ua_override)
            major = match.group(1) if match else "150"
            client = await page.context.new_cdp_session(page)
            await client.send("Network.setUserAgentOverride", {
                "userAgent": self._ua_override,
                "userAgentMetadata": {
                    "brands": [
                        {"brand": "Google Chrome", "version": major},
                        {"brand": "Chromium", "version": major},
                        {"brand": "Not?A_Brand", "version": "24"},
                    ],
                    "fullVersion": f"{major}.0.0.0",
                    "platform": "Windows", "platformVersion": "15.0.0",
                    "architecture": "x86", "model": "", "mobile": False,
                },
            })
        except Exception:
            # UA 覆盖失败不应阻断解析；退回默认 UA 继续。
            pass

    async def _trigger_play(self, page) -> None:
        """触发页面播放以让 du_web_sdk 自行签名并发出目标 baseInfo。

        优先用 Playwright 的真实点击（走 CDP 输入事件，比注入 JS click 更接近真人）；
        找不到明确的播放控件时，回退到脚本点击“播放”类元素（沿用旧行为）。
        """
        selectors = ("button[aria-label*=播放]", "button[title*=播放]",
                     "[class*=play][role=button]", "[class*=Play] button")
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count():
                    await locator.click(timeout=1200)
                    return
            except Exception:
                continue
        try:
            await page.evaluate(_TRIGGER_PLAY_JS)
        except Exception:
            pass

    async def _recover_via_headful(self, page, track_id: str):
        """无头撞验证码时切到有头，弹窗让用户手动过验证码，再在有头下重试本曲。

        不需要用户敲回车：有头窗口里过完验证码后，页面会自动放行 baseInfo，这里用
        一个较长的超时轮询到结果即恢复。切有头后本会话保持有头（惩罚态通常持续一段
        时间），避免每曲反复切换。返回 (新 page, node, last_err)。
        """
        try:
            await page.close()
        except Exception:
            pass
        if self._headless:
            print("检测到图形验证码风控：正在打开有头浏览器，请在弹出的窗口中手动通过一次"
                  "验证码，无需其它操作，通过后会自动继续下载。")
            await self._switch_to_headful()
        else:
            print("检测到图形验证码风控：请在已打开的浏览器窗口中手动通过一次验证码，"
                  "无需其它操作，通过后会自动继续下载。")
        recover_page = await self._ctx.new_page()
        # 惩罚态下页面会在用户解验证码前先回若干个 3005；恢复必须**只认成功响应、忽略
        # 中途的 3005**，一直等到用户过完验证码、baseInfo 重发成功或长超时。
        recover_timeout = max(self._timeout, 180.0)
        node = await self._await_success_through_captcha(
            recover_page, track_id, recover_timeout)
        if node:
            return recover_page, node, None
        # 这次长时间等待仍没等到用户过验证码：后续曲目不再重复白等，直接熔断。
        self._recovery_failed = True
        return recover_page, None, {"ret": None, "msg": None,
                                    "timeout": True, "captcha": True}

    async def _await_success_through_captcha(self, page, track_id: str,
                                             total_wait: float):
        """有头恢复专用：等待成功的 baseInfo，忽略过验证码前的 3005 等错误。

        关键：**不要在用户解验证码期间反复重发请求**——每次重发都会弹一个新的验证码
        把正在解的那个顶掉，造成“无限验证码”。这里加载页面后基本保持不动，让验证码
        自然出现一次；用户解完后 GeeTest 会自动重发被拦的请求，我们只被动等成功响应。
        仅当页面加载后一段时间完全没有任何 baseInfo/验证码活动时，才**只轻触一次**播放
        来促成一次请求。拿到含 playUrlList 的成功响应即返回，否则等到 total_wait 返回 None。
        """
        success = {"node": None}
        activity = {"seen": False}
        tasks: set[asyncio.Task] = set()

        async def inspect(response) -> None:
            if "baseInfo" not in response.url:
                return
            try:
                body = await response.json()
            except Exception:
                return
            result = _parse_base_info_payload(response.url, body, str(track_id))
            # 只接受成功（result[0] 为含 playUrlList 的节点），忽略 (None, err)。
            if result and result[0] and success["node"] is None:
                success["node"] = result[0]

        def on_response(response) -> None:
            if "baseInfo" in response.url or _is_captcha_url(response.url):
                activity["seen"] = True
            task = asyncio.create_task(inspect(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.on("response", on_response)
        loop = asyncio.get_running_loop()
        try:
            await page.goto(platform.SOUND_URL.format(track_id=track_id),
                            wait_until="domcontentloaded")
            deadline = loop.time() + total_wait
            triggered_once = False
            while loop.time() < deadline:
                if success["node"] is not None:
                    return success["node"]
                await asyncio.sleep(1.0)
                # 页面若已经自己发了 baseInfo / 弹了验证码，就完全交给用户去解，绝不
                # 再戳它。只有在观察窗内页面毫无动静时，才轻触一次让请求发生一次。
                if (not triggered_once and not activity["seen"]
                        and loop.time() - (deadline - total_wait) >= 5.0):
                    triggered_once = True
                    await self._trigger_play(page)
            return success["node"]
        finally:
            page.remove_listener("response", on_response)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _switch_to_headful(self) -> None:
        """关闭当前（无头）浏览器，在同一 profile/port 上以有头重开并重新接管。"""
        await self.close()               # 终止无头 Chrome、停掉 playwright
        for _ in range(50):              # 等端口真正释放，避免连到正在退出的实例
            if not _port_alive(self._port):
                break
            time.sleep(0.1)
        self._headless = False
        self._escalated = True
        self._ua_override = ""           # 有头是正常 Chrome，无需 UA 覆盖
        await self.open()

    def _raise_for(self, last_err):
        ret = last_err.get("ret") if isinstance(last_err, dict) else None
        msg = last_err.get("msg") if isinstance(last_err, dict) else None
        captcha = bool(last_err.get("captcha")) if isinstance(last_err, dict) else False
        timeout = bool(last_err.get("timeout")) if isinstance(last_err, dict) else False
        # 超时且期间弹出了图形验证码：这是风控惩罚态，headless 无法解验证码，继续
        # 冲击只会加深惩罚。按 RiskControlError 熔断，并给出可操作的恢复指引。
        if captcha:
            raise RiskControlError(
                "触发图形验证码风控：目标播放信息未返回，页面弹出了验证码。"
                "请用有头模式（chrome_headless=False）运行、在浏览器窗口中手动通过一次"
                "验证码，或改用日常浏览器的登录 Profile 后重试。",
                ret=ret,
            )
        # 纯超时（未见验证码）：可能是网络慢或页面未自动发起目标请求，可重试。
        if timeout:
            raise ApiError(
                "未能在超时内获取播放信息（未捕获到目标 baseInfo 响应）。"
                "可加 --debug 诊断，或适当调大 resolve_timeout。",
                ret=None, retryable=True,
            )
        # 3005 是复用码：实测 msg=系统繁忙时属于临时风控，而非账号无权。
        # 先按语义识别，避免把整批风控误报成不可重试的 AuthError。
        if ret == 1001 or (ret == 3005 and "系统繁忙" in str(msg or "")):
            raise RiskControlError(
                f"触发风控（ret={ret} msg={msg}）。已停止继续派发请求。",
                ret=ret,
            )
        if ret in _AUTH_RETS:
            raise AuthError(f"无权访问该音频（ret={ret} msg={msg}）。"
                            "可能需要登录或该内容在当前地区/账号下不可用。")
        if ret == 3005:
            raise AuthError(f"无权访问该音频（ret={ret} msg={msg}）。"
                            "可能需要登录或该内容在当前地区/账号下不可用。")
        raise ApiError(f"未能获取播放信息（ret={ret} msg={msg}）。可加 --debug 诊断。",
                       ret=ret)

    def _record_risk_event(self, track_id: str, started: float, outcome: str,
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
                session_id=self._risk_session_id,
                request_index=request_index,
                started_at=started_at,
                authenticated=self._authenticated,
                device_fingerprint_reset=self._device_fingerprint_was_reset,
            )
        except OSError:
            # 观测失败不能掩盖真实的平台响应或阻断正常下载。
            pass
