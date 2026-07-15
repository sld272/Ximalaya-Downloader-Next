# -*- coding: utf-8 -*-
"""浏览器设备指纹提取器（对齐 easy-sign 的 Playwright + du_web_sdk 思路）。

默认从 XDL 专用 Chrome Profile 打开喜马拉雅首页，读取
`du_web_sdk._deviceInfoCollector`。可选先清设备 Cookie / storage，让 SDK
重新生成身份后再采集——这才是「真换指纹」，不是本地改几个 ID 字段。

提取过程不向受保护播放接口发请求。换 UA/IP/Chrome 大版本后通常需要重新提取。
参考：https://github.com/liuziheng20091106/easy-sign
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

from ...config import platform
from .cookies import device_cookie_delete_targets, is_login_cookie


# 在 du_web_sdk 加载完成后回读其内部设备信息收集器。兼容 easy-sign 实测过的
# 两种暴露形态。
_EXTRACT_JS = r"""
() => {
  const s = window.du_web_sdk;
  if (!s) return { ok: false, reason: "no_du_web_sdk" };
  const collector = s._deviceInfoCollector
    || (s._checkextensions && s._checkextensions._deviceInfoCollector);
  if (!collector) {
    return {
      ok: false,
      reason: "no_collector",
      keys: Object.keys(s).slice(0, 40),
    };
  }
  return { ok: true, info: JSON.parse(JSON.stringify(collector)) };
}
"""

# 清空 origin 下可能把新旧设备身份关联起来的 storage（与 ChromeSource 一致）。
_CLEAR_STORAGE_JS = r"""
async () => {
  const result = {
    localStorageCleared: 0,
    sessionStorageCleared: 0,
    indexedDB: [],
    indexedDBError: null,
  };
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
          const done = () => {
            if (!settled) { settled = true; clearTimeout(t); resolve(); }
          };
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


@dataclass
class DeviceExtractResult:
    """一次浏览器提取的结果：指纹 + 可选 Cookie + 清理摘要。"""
    device_info: dict
    cookies: list[dict] = field(default_factory=list)
    cleared_cookie_names: list[str] = field(default_factory=list)
    storage_report: dict | None = None
    profile_dir: str = ""
    used_temp_profile: bool = False


def _launch_kwargs(
    profile_dir: str,
    chrome_path: str,
    headless: bool,
) -> dict:
    kwargs: dict[str, Any] = dict(
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
        kwargs["executable_path"] = chrome_path
    else:
        # 优先系统 Google Chrome；与 easy-sign 用 Edge channel 同类思路。
        kwargs["channel"] = "chrome"
    return kwargs


def _filter_site_cookies(cookies: list[dict], domain: str = platform.BASE) -> list[dict]:
    host = domain.replace("https://", "").replace("http://", "").split("/", 1)[0]
    out = []
    for c in cookies:
        name = str(c.get("domain") or "")
        if name.startswith("."):
            if host == name[1:] or host.endswith(name):
                out.append(c)
        elif host == name or host.endswith("." + name):
            out.append(c)
    return out


def _clear_device_cookies_in_context(ctx, page) -> list[str]:
    """通过 CDP 定向删除设备 Cookie，不改动登录等业务 Cookie。"""
    try:
        all_cookies = ctx.cookies()
    except Exception:
        return []
    targets = device_cookie_delete_targets(all_cookies)
    if not targets:
        return []

    removed: list[str] = []
    client = None
    try:
        client = ctx.new_cdp_session(page)
        client.send("Network.enable")
        for target in targets:
            try:
                client.send("Network.deleteCookie", target)
            except Exception:
                continue
            removed.append(target["name"])
    except Exception:
        return []
    finally:
        if client is not None:
            try:
                client.detach()
            except Exception:
                pass
    return sorted(set(removed))


def _read_collector(page) -> dict:
    result = page.evaluate(_EXTRACT_JS)
    if not isinstance(result, dict):
        raise RuntimeError("提取脚本返回异常，无法解析 du_web_sdk。")
    if result.get("ok") and isinstance(result.get("info"), dict):
        return result["info"]
    reason = result.get("reason") or "unknown"
    keys = result.get("keys") or []
    raise RuntimeError(
        f"页面未暴露可用的 du_web_sdk 设备收集器（{reason}）。"
        f" keys={keys[:20]!r}。可尝试 --no-headless 观察页面，或确认已打开喜马拉雅域。"
    )


def extract_device_info(
    profile_dir: str,
    chrome_path: str = "",
    headless: bool = True,
    url: str = platform.HOME_URL,
    timeout_ms: int = 60000,
    wait_ms: int = 3000,
) -> dict:
    """用 Playwright 在指定 Chrome 用户目录里打开页面，读出设备指纹字典。

    只读提取，不清理设备态。若需要「重生」指纹，请用
    `refresh_device_identity_via_browser`。
    """
    result = refresh_device_identity_via_browser(
        profile_dir=profile_dir,
        chrome_path=chrome_path,
        headless=headless,
        url=url,
        timeout_ms=timeout_ms,
        wait_ms=wait_ms,
        clear_device_state=False,
        fresh_profile=False,
    )
    return result.device_info


def refresh_device_identity_via_browser(
    profile_dir: str = "",
    chrome_path: str = "",
    headless: bool = True,
    url: str = platform.HOME_URL,
    timeout_ms: int = 60000,
    wait_ms: int = 4000,
    clear_device_state: bool = True,
    fresh_profile: bool = False,
    post_clear_wait_ms: int = 2500,
) -> DeviceExtractResult:
    """打开真实浏览器，可选清设备态后让 du_web_sdk 重生，再采集指纹与 Cookie。

    Args:
        profile_dir: 专用 Profile；`fresh_profile=True` 时忽略并使用临时目录。
        clear_device_state: 导航前清设备 Cookie + storage，再二次加载以重生身份。
        fresh_profile: 使用全新临时 Profile（完全新设备；不含登录态）。
        wait_ms: 页面 load 后等待 SDK 初始化的时间。
        post_clear_wait_ms: 清 storage 后再次 goto 等待 SDK 重生的时间。

    Returns:
        DeviceExtractResult：含 device_info、站点 Cookie、清理摘要。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "浏览器提取设备指纹需要安装 playwright：pip install playwright"
        ) from exc

    temp_dir: str | None = None
    used_temp = False
    if fresh_profile or not profile_dir:
        temp_dir = tempfile.mkdtemp(prefix="xdl-device-")
        work_profile = temp_dir
        used_temp = True
    else:
        work_profile = profile_dir
        os.makedirs(work_profile, exist_ok=True)

    cleared: list[str] = []
    storage_report: dict | None = None
    info: Optional[dict] = None
    cookies: list[dict] = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            **_launch_kwargs(work_profile, chrome_path, headless)
        )
        try:
            page = ctx.new_page()
            page.goto(url, wait_until="load", timeout=timeout_ms)
            page.wait_for_timeout(wait_ms)

            if clear_device_state:
                cleared = _clear_device_cookies_in_context(ctx, page)
                try:
                    storage_report = page.evaluate(_CLEAR_STORAGE_JS)
                except Exception as e:
                    storage_report = {"error": str(e)}
                # 清完后重新加载，让 SDK 在空 storage 上生成新设备身份
                page.goto(url, wait_until="load", timeout=timeout_ms)
                page.wait_for_timeout(post_clear_wait_ms)

            info = _read_collector(page)
            try:
                cookies = _filter_site_cookies(ctx.cookies())
            except Exception:
                cookies = []
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    if info is None:
        raise RuntimeError("未能从浏览器读取设备指纹。")

    # 临时 Profile 用完即删目录元数据；不强制 rm tree（Windows 文件锁），调用方不依赖
    return DeviceExtractResult(
        device_info=info,
        cookies=cookies,
        cleared_cookie_names=cleared,
        storage_report=storage_report if isinstance(storage_report, dict) else None,
        profile_dir=work_profile,
        used_temp_profile=used_temp,
    )


def save_device_info(device_info: dict, path: str) -> None:
    """保存设备指纹到 JSON 文件。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(device_info, f, ensure_ascii=False, indent=2)


def identity_fingerprint(device_info: dict) -> str:
    """对关键身份字段做短指纹，便于日志对比（不含完整 device_info）。"""
    parts = [
        str(device_info.get("HW5") or ""),
        str(device_info.get("GJ2") or ""),
        str(device_info.get("DP5") or ""),
        str(device_info.get("adi") or ""),
        str((device_info.get("fd2") or {}).get("xz7") or ""),
    ]
    raw = "|".join(parts).encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:16]


def summarize_extract(result: DeviceExtractResult) -> str:
    """人类可读的一行摘要（不含 Cookie/指纹值）。"""
    parts = [f"字段 {len(result.device_info)}"]
    if result.cleared_cookie_names:
        parts.append("清 Cookie: " + ", ".join(result.cleared_cookie_names[:12]))
    if isinstance(result.storage_report, dict):
        ls = int(result.storage_report.get("localStorageCleared") or 0)
        ss = int(result.storage_report.get("sessionStorageCleared") or 0)
        parts.append(f"localStorage={ls} sessionStorage={ss}")
        idb = result.storage_report.get("indexedDB") or []
        if idb:
            parts.append("IndexedDB: " + ", ".join(str(x) for x in idb[:8]))
    login = "已登录" if is_login_cookie(result.cookies) else "无登录 token"
    parts.append(login)
    if result.used_temp_profile:
        parts.append("临时 Profile")
    return "；".join(parts)
