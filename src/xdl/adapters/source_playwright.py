# -*- coding: utf-8 -*-
"""在线音源适配器（实现 Source 端口，见 docs/architecture.md §7.2）。

策略：用 Playwright 加载已登录页面，让页面自身发出带 xm-sign 的 baseInfo
请求并截获其成功响应——因此无需自行复现签名（签名由页面 du_web_sdk 生成）。
拿到 playUrlList 后用注入的 Decoder 解码为可直接下载的地址。

专辑清单同理走「页面自己签名」：在已登录的专辑页内同源 fetch getTracksList
逐页取全集（未登录仅能取到第一页）。

会话复用：open()/close() 之间长驻同一浏览器/上下文/页面，专辑下载逐集导航
复用它，避免每集冷启动浏览器（架构 §7.1「进程长驻、反复复用」）。单曲场景
不显式 open 时，get_track/get_album 自行临时起停浏览器，行为与此前一致。

注：签名能力本应抽象为 SignProvider 端口（架构 §7.1），但 MVP 采用「让页面
自己签名」的方案，签名被隐含在本适配器内。后续若实现本地签名（Node/纯算），
再把 SignProvider 抽出并接入降级链。
"""
from __future__ import annotations

import time

import requests

from ..config import platform
from ..domain import Track, PlayUrl, Album, AlbumTrack
from ..errors import ApiError, AuthError, NetworkError
from ..ports import Decoder

# 这些 ret 码表示无权访问/地区限制等鉴权类问题
_AUTH_RETS = {3005, 927}
_LIST_TIMEOUT = 30        # 曲目清单 HTTP 超时（秒）
_MAX_PAGES = 2000         # 翻页安全上限


class PlaywrightSource:
    def __init__(self, cookiejar, decoder: Decoder, resolve_timeout: int = 40):
        self._jar = cookiejar
        self._decoder = decoder
        self._timeout = resolve_timeout
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # ---- 会话生命周期（Source 端口） ----
    def open(self) -> None:
        """长驻一个无头浏览器会话，供批量解析复用。"""
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        storage = self._jar.state_path()
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True, args=platform.BROWSER_ARGS)
        self._context = self._browser.new_context(storage_state=storage, user_agent=platform.UA)
        self._context.add_init_script(platform.INIT_HOOK_JS)
        self._page = self._context.new_page()
        # 匿名访问时先访问首页预热，让服务端下发匿名 Cookie（反爬/设备标识）
        if not storage:
            self._page.goto(platform.HOME_URL, wait_until="domcontentloaded")
            self._page.wait_for_timeout(2500)

    def close(self) -> None:
        for obj, meth in ((self._context, "close"), (self._browser, "close"),
                          (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:
                pass
        self._pw = self._browser = self._context = self._page = None

    def _session(self):
        """返回 (page, owns)：owns 表示本次调用临时开启、用毕需关闭。"""
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
        from playwright.sync_api import sync_playwright

        self._jar.ensure_dir()
        dest = self._jar.location()
        print("即将打开浏览器，请完成登录（扫码或账号密码）。")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(user_agent=platform.UA)
            page = context.new_page()
            page.goto(platform.HOME_URL, wait_until="domcontentloaded")
            input(">>> 登录完成后回到这里按回车保存登录态: ")
            context.storage_state(path=dest)
            browser.close()
        return dest

    # ---- 内部：在给定 page 上导航并截获 baseInfo ----
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
