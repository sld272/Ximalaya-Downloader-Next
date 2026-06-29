# -*- coding: utf-8 -*-
"""在线音源适配器（实现 Source 端口，见 docs/architecture.md §7.1/§7.2）。

单曲解析走「让页面自己签名」：加载已登录的 /sound 页，页面内 du_web_sdk 生成
xm-sign 并发出 baseInfo 请求，注入的 XHR 钩子截获其成功响应（含 playUrlList）。

关键（与最初 MVP 的重大差异）：平台 du_web_sdk 现对 baseInfo 加了**自动化环境
风控**——Playwright 直接 launch 的 Chromium（无论有头/无头、是否 stealth）都会被
判为机器人，返回 1001/3005「系统繁忙」。实测：正常启动的真实 Chrome 能 ret 0。
因此这里**自己以干净方式启动真实 Chrome**（仅开调试端口、不带自动化标志），再用
Playwright `connect_over_cdp` 接管。登录态持久化在专用 Chrome 用户配置目录里
（取代早期的 storage_state auth.json）。

专辑清单不经浏览器：走免签的「非 v1」getTracksList 接口纯 HTTP 翻页。
"""
from __future__ import annotations

import os
import socket
import subprocess
import time

import requests

from ..config import platform
from ..domain import Track, PlayUrl, Album, AlbumTrack
from ..errors import ApiError, AuthError, NetworkError, ConfigError
from ..ports import Decoder

# 这些 ret 码表示无权访问/地区限制等鉴权类问题
_AUTH_RETS = {3005, 927}
_LIST_TIMEOUT = 30        # 曲目清单 HTTP 超时（秒）
_MAX_PAGES = 2000         # 翻页安全上限


def _port_alive(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


class ChromeSource:
    """经 CDP 接管真实 Chrome 解析单曲；HTTP 取专辑清单。"""

    def __init__(self, decoder: Decoder, chrome_path: str, profile_dir: str,
                 port: int = 9222, resolve_timeout: int = 40, headless: bool = True):
        self._decoder = decoder
        self._chrome_path = chrome_path
        self._profile_dir = profile_dir
        self._port = port
        self._timeout = resolve_timeout
        self._headless = headless
        self._proc = None
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # ---- 会话生命周期（Source 端口） ----
    def open(self) -> None:
        if self._page is not None:
            return
        self._require_chrome()
        os.makedirs(self._profile_dir, exist_ok=True)
        if not _port_alive(self._port):
            self._launch_chrome(headless=self._headless)
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self._port}")
        except Exception as e:
            self.close()
            raise NetworkError(f"接管 Chrome 失败（端口 {self._port}）: {e}") from e
        self._context = (self._browser.contexts[0] if self._browser.contexts
                         else self._browser.new_context())
        self._context.add_init_script(platform.INIT_HOOK_JS)
        self._page = (self._context.pages[0] if self._context.pages
                      else self._context.new_page())

    def close(self) -> None:
        try:
            if self._pw is not None:
                self._pw.stop()
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
        self._proc = self._pw = self._browser = self._context = self._page = None

    def _session(self):
        """返回 (page, owns)：owns 表示本次临时开启、用毕需关闭。"""
        owns = self._page is None
        if owns:
            self.open()
        return self._page, owns

    # ---- Source 端口：单曲 ----
    def get_track(self, track_id: str) -> Track:
        page, owns = self._session()
        try:
            node, last_err = self._capture_base_info(page, track_id)
            if not node:
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

            return Track(
                track_id=str(track_id),
                title=node.get("title") or str(track_id),
                play_urls=play_urls,
                is_paid=bool(node.get("isPaid")),
                is_authorized=bool(node.get("isAuthorized", True)),
            )
        finally:
            if owns:
                self.close()

    # ---- Source 端口：专辑清单 ----
    def get_album(self, album_id: str) -> Album:
        """取专辑曲目清单。走免签的「非 v1」getTracksList 接口，纯 HTTP 翻页，
        无需浏览器/登录（清单为公开信息；逐集 playUrl 仍在下载时经浏览器解析）。"""
        album_id = str(album_id)
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
            time.sleep(0.3)   # 克制：翻页间略作停顿

        if not tracks:
            raise ApiError("专辑无可下载曲目或不存在。")
        return Album(album_id=album_id, title=title or album_id,
                     total=total or len(tracks), tracks=tracks)

    # ---- 适配器特有：交互式登录 ----
    def interactive_login(self) -> str:
        """以有头真实 Chrome 打开首页供用户登录；会话持久化在专用配置目录。"""
        self._require_chrome()
        os.makedirs(self._profile_dir, exist_ok=True)
        print("即将打开 Chrome，请在其中完成登录（扫码或账号密码）。")
        args = [self._chrome_path, f"--user-data-dir={self._profile_dir}",
                *platform.CHROME_LAUNCH_ARGS, platform.HOME_URL]
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            input(">>> 登录完成后回到这里按回车结束（会话已存入专用 Chrome 配置）: ")
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return self._profile_dir

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
        for _ in range(75):                 # 最多等 ~15s
            if _port_alive(self._port):
                return
            time.sleep(0.2)
        self.close()
        raise NetworkError(f"Chrome 调试端口 {self._port} 未就绪（启动超时）。")

    def _capture_base_info(self, page, track_id: str):
        # 导航会重跑 init 钩子，自动把 window.__xmcap/__xmerr 重置为 null
        page.goto(platform.SOUND_URL.format(track_id=track_id),
                  wait_until="domcontentloaded")

        node = None
        last_err = None
        start = time.time()
        tried_click = False
        while time.time() - start < self._timeout:
            node = page.evaluate("window.__xmcap")
            if node:
                break
            last_err = page.evaluate("window.__xmerr")
            page.wait_for_timeout(400)
            if not tried_click and time.time() - start > 3:
                page.evaluate(platform.TRIGGER_PLAY_JS)   # 自动播放被拦时点一下
                tried_click = True
        return node, last_err

    def _raise_for(self, last_err):
        ret = last_err.get("ret") if isinstance(last_err, dict) else None
        msg = last_err.get("msg") if isinstance(last_err, dict) else None
        if ret in _AUTH_RETS:
            raise AuthError(f"无权访问该音频（ret={ret} msg={msg}）。"
                            "可能需要登录或该内容在当前地区/账号下不可用。")
        if ret == 1001:
            raise ApiError(f"触发风控（ret=1001 msg={msg}）。稍后重试或确认登录态有效。",
                           ret=1001, retryable=True)
        raise ApiError(f"未能获取播放信息（ret={ret} msg={msg}）。可加 --debug 诊断。",
                       ret=ret)
