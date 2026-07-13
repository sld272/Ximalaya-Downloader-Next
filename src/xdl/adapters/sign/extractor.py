# -*- coding: utf-8 -*-
"""浏览器设备指纹提取器（供 `xdl extract-device` 使用）。

打开用户已有的 XDL 专用 Chrome Profile（与 `xdl login` 共用），加载喜马拉雅首页，
通过 `du_web_sdk._deviceInfoCollector` 拿到一份真实设备指纹；保存为 JSON 后
`PySignProvider` 可反复使用，无需再开浏览器即能生成 xm-sign。

提取过程只读，不改写页面 JS、不模拟任何用户操作，亦不向受保护接口发请求。
指纹与"该 Profile 在该机器上的浏览器环境"绑定；换 UA/IP/Chrome 大版本后可能
需要重新提取。
"""
from __future__ import annotations

import json
import os
from typing import Optional

from ...config import platform


# 在 du_web_sdk 加载完成后回读其内部设备信息收集器。尝试两种暴露形态：
#  1) `_deviceInfoCollector` 直接挂在 du_web_sdk 上
#  2) 通过 `_checkextensions._deviceInfoCollector`（实测 easy-sign 模板）
# 兼容 du_web_sdk 不同版本的内部结构。
_EXTRACT_JS = r"""
() => {
  const s = window.du_web_sdk;
  if (!s) return null;
  const collector = s._deviceInfoCollector
    || (s._checkextensions && s._checkextensions._deviceInfoCollector);
  if (!collector) return null;
  return JSON.parse(JSON.stringify(collector));
}
"""


def extract_device_info(
    profile_dir: str,
    chrome_path: str = "",
    headless: bool = True,
    url: str = platform.HOME_URL,
    timeout_ms: int = 60000,
    wait_ms: int = 3000,
) -> dict:
    """用 Playwright 在指定 Chrome 用户目录里打开页面，读出设备指纹字典。

    Args:
        profile_dir: Chrome 用户配置目录（即 `xdl login` 用的 `~/.xdl/chrome-profile`）。
        chrome_path: Chrome 可执行文件路径；为空时由 Settings 默认探测。
        headless: 是否无头；调试可置 False 看到浏览器。
        url: 打开页面，默认喜马拉雅首页（必含 du_web_sdk）。
        timeout_ms: 页面 goto 超时（毫秒）。
        wait_ms: 加载完后等待 SDK 初始化的额外时间（毫秒）。

    Raises:
        RuntimeError: 未安装 Playwright 或页面未暴露 du_web_sdk。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "提取设备指纹需要安装 playwright：pip install playwright") from exc

    os.makedirs(profile_dir, exist_ok=True)
    info: Optional[dict] = None

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
        # 用 Playwright 管理的 Chrome 会出现自动化痕迹，但提取只读、不发受保护请求，
        # 不影响后续纯算签名。优先用系统 Google Chrome，回退到 Playwright 的 chromium。
        launch_kwargs["channel"] = "chrome"

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(**launch_kwargs)
        try:
            page = ctx.new_page()
            page.goto(url, wait_until="load", timeout=timeout_ms)
            page.wait_for_timeout(wait_ms)
            info = page.evaluate(_EXTRACT_JS)
            if info is None:
                # 也许 SDK 已加载但 `_deviceInfoCollector` 命名不同；重试一次。
                info = page.evaluate(
                    "() => window.du_web_sdk ? "
                    "JSON.parse(JSON.stringify(window.du_web_sdk)) : null"
                )
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    if info is None:
        raise RuntimeError(
            "页面未加载 du_web_sdk，无法提取设备指纹。"
            "请确认已 `xdl login` 并在喜马拉雅域打开页面。")
    return info


def save_device_info(device_info: dict, path: str) -> None:
    """保存设备指纹到 JSON 文件。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(device_info, f, ensure_ascii=False, indent=2)