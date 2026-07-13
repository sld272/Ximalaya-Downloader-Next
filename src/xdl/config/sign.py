# -*- coding: utf-8 -*-
"""xm-sign 生成算法所需的常量（见 docs/architecture.md §7.1）。

算法与 `liuziheng20091106/easy-sign` 仓库的 `xm_sign_toolkit/core.py` 一致：

    device_info (dict)
        │  JSON 序列化 -> URL 编码 -> zlib 压缩
        ▼
    AES-ECB 加密(KEY) -> Base64
        │
        ▼
    POST https://hdaa.shuzilm.cn/report?v=1.2.0&e=1&c=1&r=<uuid>
        │
        ▼
    Base64 解码 -> AES-ECB 解密(KEY) -> JSON
        │
        ▼
    取 cadd + sid -> xm-sign = "{cadd}&&{sid}"

KEY 来自 `du_web_sdk` 的 `_getDeviceKey(0)`，与平台 SDK 内嵌的硬编码密钥一致。
HOST/REPORT_URL 是设备指纹上报服务（数划算 hdaa）的固定端点。
"""
from __future__ import annotations

import os

from .paths import xdl_home

# du_web_sdk 内 _getDeviceKey(0) 返回的硬编码密钥
KEY = "m9ZtRrz:qujT8@da"

# 受保护播放信息接口：纯 HTTP 路径调用 baseInfo 时走这里。
# 实测页面 `/sound/{id}` 发出的真实端点是 `/mobile-playpage/track/v3/baseInfo/{ms_ts}`，
# 带查询 `device=www2 & trackId=... & trackQualityLevel=1`。`{ts}` 由调用方在每次请求
# 时填入当前毫秒时间戳（见 HttpSource._build_base_info_url）。之前误用的
# `/revision/track/v1/baseInfo` 会被网关直接 404，返回 HTML 而不是 JSON。
BASE_INFO_URL = "https://www.ximalaya.com/mobile-playpage/track/v3/baseInfo/{ts}"
BASE_INFO_DEVICE = "www2"
BASE_INFO_QUALITY_LEVEL = 1

# 设备指纹上报服务端点。`r` 参数为随机 uuid，服务端据此关联上报与下发。
HDAA_HOST = "hdaa.shuzilm.cn"
HDAA_REPORT_URL = f"https://{HDAA_HOST}/report?v=1.2.0&e=1&c=1&r={{uuid}}"

# du_web_sdk 当前版本号（写入 device_info.GF9）
SDK_VERSION = "2.0.0"

# 兼容旧调用方的构造参数默认值。当前实现每次使用同一次上报响应中的 cadd/sid，
# 不再缓存响应；保留常量避免破坏从 xdl.config.sign 导入它的代码。
SIGN_CACHE_TTL_SECONDS = 30 * 60


def default_device_info_path() -> str:
    """存放用户提取的设备指纹 JSON 的默认路径（~/.xdl/device-info.json）。"""
    return os.path.join(xdl_home(), "device-info.json")


def default_cookies_cache_path() -> str:
    """从 Chrome profile 中导出的登录 Cookie 缓存路径（~/.xdl/cookies.json）。"""
    return os.path.join(xdl_home(), "cookies.json")


def load_default_device_info() -> dict:
    """加载内置的公共设备指纹模板（与 easy-sign 的 public_template.json 同源）。

    模板已经清理掉 `HeadlessChrome` UA 指纹（替换为正常 Chrome 125 UA），
    `Zf5` 时间戳留作 0（PySignProvider 在每次 sign 前会刷新为当前时间）。
    适合作为"开箱可用"的兜底指纹；用户可用 `xdl extract-device` 从自己日常
    Chrome 提取更贴近真实环境的指纹覆盖它。
    """
    import json
    template_path = os.path.join(os.path.dirname(__file__),
                                  "device_info_default.json")
    with open(template_path, "r", encoding="utf-8") as f:
        return json.load(f)
