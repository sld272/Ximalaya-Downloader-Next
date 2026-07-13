# -*- coding: utf-8 -*-
"""从 Chrome 用户目录提取登录 Cookie（供 HttpSource 纯 HTTP 路径使用）。

XDL 的 `xdl login` 已用专用 Chrome Profile 持久化了登录态。HttpSource 不走 CDP
接管、也不发受保护请求——它只需要把那批登录 Cookie 读出来塞进自己的 `requests`
请求头里。这里用 Playwright 的 `launch_persistent_context` 短暂启动用户已有的
Profile（不复用 `xdl login` 启动的进程），默认直接读取 `cookies()`，不导航到站点。
只有显式指定 `visit_home=True` 时才访问首页；导出后立即关闭上下文。

也提供 JSON 缓存（`~/.xdl/cookies.json`）——一次提取后 HttpSource 可直接读 JSON，
不必每次启动都开一遍浏览器。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Iterable

from ...config import platform

_LOGIN_COOKIE_SUFFIX = "&_token"


def is_login_cookie(cookies: Iterable[dict]) -> bool:
    """只判断登录 token 是否存在；不读 value。"""
    return any(
        str(c.get("name") or "").endswith(_LOGIN_COOKIE_SUFFIX) and bool(c.get("value"))
        for c in cookies
    )


def extract_cookies_from_profile(
    profile_dir: str,
    chrome_path: str = "",
    headless: bool = True,
    cookie_domain: str = platform.BASE,
    timeout_ms: int = 30000,
    wait_ms: int = 1500,
    visit_home: bool = False,
) -> list[dict]:
    """短暂启动 Chrome（共用 `~/.xdl/chrome-profile`），读出 ximalaya.com 域 Cookie。

    返回的 Cookie 形如：
        [{"name": "1&_token", "value": "...", "domain": ".ximalaya.com",
          "path": "/", "httpOnly": True, "secure": True, ...}, ...]

    与 `requests` 兼容（用 `name/value` 拼 Cookie 头即可，安全标志由 HTTPS 保障）。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "提取登录 Cookie 需要安装 Playwright：pip install playwright") from exc

    if not os.path.isdir(profile_dir):
        raise FileNotFoundError(
            f"Chrome Profile 目录不存在: {profile_dir}。请先运行 `xdl login` 创建。")

    launch_kwargs = dict(
        headless=headless,
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        args=[
            "--no-first-run",
            "--no-default-browser-check",
            "--mute-audio",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-blink-features=AutomationControlled",
        ],
        user_data_dir=profile_dir,
    )
    if chrome_path:
        launch_kwargs["executable_path"] = chrome_path
    else:
        launch_kwargs["channel"] = "chrome"

    cookies: list[dict] = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(**launch_kwargs)
        try:
            if visit_home:
                page = ctx.new_page()
                try:
                    page.goto(platform.HOME_URL, wait_until="domcontentloaded",
                              timeout=timeout_ms)
                    page.wait_for_timeout(wait_ms)
                except Exception:
                    # 显式导航失败时仍尝试读已落盘 Cookie。
                    pass
            # 读全部域的 Cookie，再按目标 host 过滤；避免错过跨子域的登录 Cookie
            all_cookies = ctx.cookies()
            cookies = [c for c in all_cookies
                       if _cookie_matches(c, cookie_domain)]
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    return cookies


def _cookie_matches(cookie: dict, domain: str) -> bool:
    """简单判断 cookie 是否属于 ximalaya.com（含子域）。"""
    name = str(cookie.get("domain") or "")
    host = domain.replace("https://", "").replace("http://", "")
    # 去掉 host 里的端口和路径
    host = host.split("/", 1)[0]
    if name.startswith("."):
        return host == name[1:] or host.endswith(name)
    return host == name


def build_cookie_header(cookies: Iterable[dict]) -> str:
    """把 cookie 列表组装成 `Cookie:` 请求头的值。"""
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies
                     if c.get("name") and c.get("value") is not None)


def save_cookies(cookies: list[dict], path: str) -> None:
    """原子地缓存 Cookie 到 JSON（value 含登录态，按文件权限保护）。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    slim = [
        {
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain"),
            "path": c.get("path", "/"),
        }
        for c in cookies
        if c.get("name") and c.get("value")
    ]
    fd, temp_path = tempfile.mkstemp(
        prefix=".cookies-", suffix=".tmp", dir=directory or ".",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(slim, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def load_cached_cookies(path: str, max_age_seconds: int = 1800) -> list[dict] | None:
    """若缓存文件存在且足够新鲜则返回内容，否则返回 None。"""
    if not os.path.exists(path):
        return None
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return None
    if age > max_age_seconds:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (OSError, ValueError):
        pass
    return None
