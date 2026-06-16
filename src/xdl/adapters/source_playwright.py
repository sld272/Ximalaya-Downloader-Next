# -*- coding: utf-8 -*-
"""在线音源适配器（实现 Source 端口，见 docs/architecture.md §7.2）。

策略：用 Playwright 加载已登录页面，让页面自身发出带 xm-sign 的 baseInfo
请求并截获其成功响应——因此无需自行复现签名（签名由页面 du_web_sdk 生成）。
拿到 playUrlList 后用注入的 Decoder 解码为可直接下载的地址。

注：签名能力本应抽象为 SignProvider 端口（架构 §7.1），但 MVP 采用「让页面
自己签名」的方案，签名被隐含在本适配器内。后续若实现本地签名（Node/纯算），
再把 SignProvider 抽出并接入降级链。
"""
from __future__ import annotations

import time

from ..config import platform
from ..domain import Track, PlayUrl
from ..errors import ApiError, AuthError
from ..ports import Decoder

# 这些 ret 码表示无权访问/地区限制等鉴权类问题
_AUTH_RETS = {3005, 927}


class PlaywrightSource:
    def __init__(self, cookiejar, decoder: Decoder, resolve_timeout: int = 40):
        self._jar = cookiejar
        self._decoder = decoder
        self._timeout = resolve_timeout

    # ---- Source 端口 ----
    def get_track(self, track_id: str) -> Track:
        node, last_err = self._capture_base_info(track_id)
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

    # ---- 内部：驱动浏览器截获 baseInfo ----
    def _capture_base_info(self, track_id: str):
        from playwright.sync_api import sync_playwright

        storage = self._jar.state_path()
        node = None
        last_err = None
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=platform.BROWSER_ARGS)
            context = browser.new_context(storage_state=storage, user_agent=platform.UA)
            context.add_init_script(platform.INIT_HOOK_JS)
            page = context.new_page()

            # 匿名访问时先访问首页预热，让服务端下发匿名 Cookie（反爬/设备标识）
            if not storage:
                page.goto(platform.HOME_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)

            page.goto(platform.SOUND_URL.format(track_id=track_id),
                      wait_until="domcontentloaded")

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
            browser.close()
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
