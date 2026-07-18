# -*- coding: utf-8 -*-
"""跨前端复用的本地诊断与凭据维护操作。"""
from __future__ import annotations

import os
import time

from ..adapters import PySignProvider
from ..adapters import sign as sign_tools
from ..errors import AuthError
from ..settings import Settings


def generate_signatures(device_info_path: str | None = None,
                        repeat: int = 1) -> dict:
    """生成一组 xm-sign 冒烟值；与下载播放信息无关。"""
    if repeat < 1:
        raise ValueError("重复次数必须大于 0。")
    signer = PySignProvider(device_info_path=device_info_path)
    signer.open()
    try:
        values = [signer.sign() for _ in range(repeat)]
    finally:
        signer.close()
    return {"repeat": repeat, "values": values}


def extract_device_identity(settings: Settings, *, output: str | None = None,
                            profile: str | None = None, headless: bool = True,
                            refresh: bool = False,
                            fresh_profile: bool = False) -> dict:
    """采集设备信息并只返回不含 Cookie/原始指纹值的摘要。"""
    result = sign_tools.refresh_device_identity_via_browser(
        profile_dir=profile or settings.chrome_profile_dir,
        chrome_path=settings.chrome_path,
        headless=headless,
        clear_device_state=refresh,
        fresh_profile=fresh_profile,
    )
    target = output or settings.device_info_path
    sign_tools.save_device_info(result.device_info, target)
    return {
        "output_path": target,
        "field_count": len(result.device_info),
        "identity": sign_tools.identity_fingerprint(result.device_info),
        "summary": sign_tools.summarize_extract(result),
        "used_temp_profile": result.used_temp_profile,
    }


def refresh_login_cookies(settings: Settings, *, headless: bool = True) -> dict:
    """从专用 Profile 刷新登录 Cookie；匿名结果不会覆盖现有缓存。"""
    cookies = sign_tools.extract_cookies_from_profile(
        profile_dir=settings.chrome_profile_dir,
        chrome_path=settings.chrome_path,
        headless=headless,
    )
    if not sign_tools.is_login_cookie(cookies):
        raise AuthError(
            "专用 Chrome Profile 中未发现登录 token（1&_token）；"
            "未覆盖现有 Cookie 缓存。"
        )
    sign_tools.save_cookies(cookies, settings.cookies_cache_path)
    return {
        "output_path": settings.cookies_cache_path,
        "cookie_count": len(cookies),
        "authenticated": True,
    }


def login_cache_status(settings: Settings) -> dict:
    """只报告本地缓存是否含登录 token，不暴露 Cookie 名或值。"""
    path = settings.cookies_cache_path
    cookies = sign_tools.load_cached_cookies(path, max_age_seconds=10**12) or []
    exists = os.path.isfile(path)
    age_seconds = None
    if exists:
        try:
            modified = int(os.path.getmtime(path))
            age_seconds = max(0, int(time.time()) - modified)
        except OSError:
            age_seconds = None
    return {
        "authenticated": sign_tools.is_login_cookie(cookies),
        "cache_exists": exists,
        "cache_age_seconds": age_seconds,
        "profile_exists": os.path.isdir(settings.chrome_profile_dir),
    }
