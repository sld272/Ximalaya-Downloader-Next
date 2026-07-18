# -*- coding: utf-8 -*-
"""纯 Python 实现 xm-sign 生成（实现 SignProvider 端口）。

算法与 `liuziheng20091106/easy-sign` 的 `xm_sign_toolkit/core.py` 一致：

    device_info(dict) ─JSON序列化─► URL编码 ─► zlib压缩 ─► AES-ECB加密 ─► Base64
        │
        ▼
    POST hdaa.shuzilm.cn/report?v=1.2.0&e=1&c=1&r=<uuid>  (application/octet-stream)
        │
        ▼
    Base64解码 ─► AES-ECB解密 ─► JSON ─► 取 cadd+sid ─► xm-sign = "cadd&&sid"

本模块只生成一个请求签名字段；它不替代登录 Cookie、内容授权或服务端的风险判断。
签名端点和目标接口均可能变化，离线单元测试只能验证本地序列化与响应解析，不能证明
真实服务端会接受请求。
"""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import threading
import time
import uuid
import zlib

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from ...config import sign as sign_conf
from ...errors import SignError

# JS encodeURIComponent 对部分保留字符的特殊处理。`safe` 这一串与 du_web_sdk
# 内部 encodeURIComponent 行为对齐（详见 easy-sign core.string_to_uint8_array）。
_URL_SAFE_CHARS = ")!~*'("


def _json_dumps_compact(data: dict) -> str:
    """与 du_web_sdk 内部 JSON.stringify 行为一致的紧凑序列化。"""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _decode_uri_special(encoded: str) -> str:
    """模拟 JS `decodeURIComponent`：兼容 %uXXXX 与 %XX。"""
    out: list[str] = []
    i, n = 0, len(encoded)
    while i < n:
        ch = encoded[i]
        if ch == "%":
            # %uXXXX
            if i + 5 <= n and encoded[i + 1] == "u":
                h = encoded[i + 2 : i + 6]
                if re.match(r"^[0-9A-Fa-f]{4}$", h):
                    out.append(chr(int(h, 16)))
                    i += 6
                    continue
            # %XX
            if i + 2 <= n:
                h = encoded[i + 1 : i + 3]
                if re.match(r"^[0-9A-Fa-f]{2}$", h):
                    out.append(chr(int(h, 16)))
                    i += 3
                    continue
        out.append(ch)
        i += 1
    return "".join(out)


def _string_to_uint8(text: str) -> bytes:
    """按 du_web_sdk 的 URL 编码规则把字符串转成 bytes。"""
    encoded = requests.utils.quote(text, safe=_URL_SAFE_CHARS)
    decoded = _decode_uri_special(encoded)
    return bytes(ord(c) for c in decoded)


def _compress(data: bytes, level: int = 6) -> bytes:
    return zlib.compress(data, level=level)


def _aes_encrypt(plaintext: bytes, key: str) -> bytes:
    cipher = AES.new(key.encode(), AES.MODE_ECB)
    return cipher.encrypt(pad(plaintext, 16))


def _aes_decrypt(ciphertext: bytes, key: str) -> bytes:
    cipher = AES.new(key.encode(), AES.MODE_ECB)
    return unpad(cipher.decrypt(ciphertext), 16)


def _process_payload(device_info: dict, key: str) -> bytes:
    """把 device_info 加工成上报用的二进制 body（与 easy-sign `get_process_data` 等价）。

    流程：JSON 序列化 → URL 编码 → zlib 压缩 → AES-ECB(PKCS7) 加密 → 返回**原始
    AES 密文 bytes**。easy-sign 这里写的是 `base64.b64decode(base64.b64encode(...))`，
    即对密文做一次 base64 编码再立即解码——一次来回等于什么都不做，最终 `data=`
    收到的是原始密文 bytes。我们去掉这一来回，直接返回密文；语义等价。
    """
    json_str = _json_dumps_compact(device_info)
    uint8 = _string_to_uint8(json_str)
    compressed = _compress(uint8)
    return _aes_encrypt(compressed, key)


def _refresh_zf5(device_info: dict) -> dict:
    """返回一份将 Zf5 刷新为当前毫秒时间戳的副本。"""
    info = copy.deepcopy(device_info)
    info["Zf5"] = int(time.time() * 1000)
    return info


def _atbash(text: str) -> str:
    """du_web_sdk 对 UA 做字母表反转（A↔Z）后再 Base64 写入 ew1.Le3。"""
    out: list[str] = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(25 - (ord(ch) - 65) + 65))
        elif "a" <= ch <= "z":
            out.append(chr(25 - (ord(ch) - 97) + 97))
        else:
            out.append(ch)
    return "".join(out)


def _encode_le3(user_agent: str) -> str:
    """与 du_web_sdk 一致：Base64(atbash(UA))，无填充 '='。"""
    return base64.b64encode(_atbash(user_agent).encode("utf-8")).decode("ascii").rstrip("=")


def user_agent_from_device_info(device_info: dict | None) -> str | None:
    """从 device_info.ew1 还原完整 User-Agent；无法还原时返回 None。"""
    if not isinstance(device_info, dict):
        return None
    ew1 = device_info.get("ew1")
    if not isinstance(ew1, dict):
        return None
    yv2 = str(ew1.get("yV2") or "")
    if not yv2:
        return None
    if yv2.startswith("Mozilla/") or yv2.startswith("Mozilla "):
        return yv2
    wg7 = str(ew1.get("Wg7") or "Mozilla")
    return f"{wg7}/{yv2}"


def sanitize_device_info(device_info: dict) -> tuple[dict, bool]:
    """去掉 device_info 中最明显的无头自动化 UA 痕迹。

    headless 提取的指纹常把 `navigator.userAgent` 写成 `HeadlessChrome/…`。
    若原样上报 hdaa，服务端会把该设备标成自动化环境；换身若仍走 headless
    采集，还会把同一痕迹写回去。这里只改写 UA 相关字段，不碰设备 ID。

    Returns:
        (sanitized_copy, changed)
    """
    if not isinstance(device_info, dict) or not device_info:
        return device_info, False
    info = copy.deepcopy(device_info)
    ew1 = info.get("ew1")
    if not isinstance(ew1, dict):
        return info, False

    yv2 = str(ew1.get("yV2") or "")
    le3 = str(ew1.get("Le3") or "")
    changed = False

    if "HeadlessChrome" in yv2:
        ew1["yV2"] = yv2.replace("HeadlessChrome", "Chrome")
        changed = True
    elif "Headless" in yv2:
        # 兜底：其它 Headless* 令牌
        ew1["yV2"] = yv2.replace("Headless", "")
        changed = True

    ua = user_agent_from_device_info({"ew1": ew1})
    if ua:
        # Le3 可能仍编码着旧的 Headless UA（即便 yV2 已人工改过）
        new_le3 = _encode_le3(ua)
        needs_le3 = False
        if le3 != new_le3:
            if changed:
                needs_le3 = True
            elif le3:
                try:
                    pad = "=" * ((4 - len(le3) % 4) % 4)
                    decoded = _atbash(
                        base64.b64decode(le3 + pad).decode("utf-8", "replace")
                    )
                    needs_le3 = "Headless" in decoded
                except Exception:
                    needs_le3 = False
        if needs_le3:
            ew1["Le3"] = new_le3
            changed = True

    if changed:
        info["ew1"] = ew1
    return info, changed


class PySignProvider:
    """纯算 xm-sign 生成器：device_info → 上报 → cadd&&sid。

    用法：
        signer = PySignProvider(device_info_path=".../device-info.json")
        signer.open()
        try:
            xm_sign = signer.sign()   # -> "{cadd}&&{sid}"
        finally:
            signer.close()

    `device_info_path` 缺省走 `config/sign.default_device_info_path()`（~/.xdl/device-info.json）；
    文件不存在时自动回退到内置模板（`config/device_info_default.json`）。

    每次 `sign()` 都只上报一次，并直接使用该次响应成对返回的 `cadd` 与 `sid`。
    旧实现缓存 `cadd`，但为了取得新 `sid` 仍然每次上报，不仅没有减少请求，还可能在
    服务端更新 `cadd` 时拼出不匹配的一对值，因此已取消这层无效缓存。
    """

    def __init__(
        self,
        device_info_path: str | None = None,
        key: str = sign_conf.KEY,
        report_url: str = sign_conf.HDAA_REPORT_URL,
        cache_ttl: int = sign_conf.SIGN_CACHE_TTL_SECONDS,
        http_timeout: int = 15,
        user_agent: str | None = None,
    ):
        self._device_info_path = device_info_path or sign_conf.default_device_info_path()
        self._key = key
        self._report_url = report_url
        # 保留 cache_ttl 参数以兼容已有调用方；当前实现不缓存签名响应。
        _ = cache_ttl
        self._http_timeout = http_timeout
        self._user_agent = user_agent
        # 设备指纹只加载一次；可通过 reload() 替换（实验换身）。
        self._device_info: dict | None = None
        self._lock = threading.RLock()

    # ---- SignProvider 端口 ----
    def open(self) -> None:
        with self._lock:
            if self._device_info is not None:
                return
            self._device_info = self._load_device_info()

    def close(self) -> None:
        with self._lock:
            self._device_info = None

    def sign(self) -> str:
        """返回形如 "{cadd}&&{sid}" 的 xm-sign 字符串。

        每次调用都打一次 hdaa 上报，并使用同一响应中的 cadd 与 sid。
        失败抛 `SignError`（可重试）。
        """
        with self._lock:
            if self._device_info is None:
                self.open()
            cadd, sid = self._fresh_report()
            return f"{cadd}&&{sid}"

    def reload(self, device_info: dict | None = None) -> None:
        """替换当前设备指纹。

        - `device_info is None`：重新从路径/内置模板加载；
        - 传入 dict：使用该深拷贝，不写回磁盘。

        供 HTTP 路径「换身」实验调用；默认下载链路不会自动调用。
        传入的 dict 会先做 HeadlessChrome UA 消毒，避免换身把无头痕迹写回。
        """
        with self._lock:
            if device_info is None:
                self._device_info = self._load_device_info()
            else:
                if not isinstance(device_info, dict) or not device_info:
                    raise SignError("reload 需要非空 device_info dict")
                cleaned, changed = sanitize_device_info(device_info)
                if changed:
                    print(
                        "[warn] 换身得到的 device_info 含 HeadlessChrome，"
                        "已在内存中改写为 Chrome 后再 reload。"
                    )
                self._device_info = cleaned

    # ---- 内部 ----
    def _load_device_info(self) -> dict:
        info = None
        path = self._device_info_path
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    info = json.load(f)
            except (OSError, ValueError) as e:
                print(f"[warn] 设备指纹文件 {path} 读取失败 ({e})，回退到内置模板。")
        if info is None:
            info = sign_conf.load_default_device_info()
        cleaned, changed = sanitize_device_info(info)
        if changed:
            print(
                "[warn] device_info 含 HeadlessChrome 自动化 UA；"
                "已在上报前改写为 Chrome。"
                "建议用有头浏览器重新 `xdl extract-device --no-headless`。"
            )
        return cleaned

    def _report_user_agent(self) -> str:
        """hdaa 请求 UA：优先构造参数，否则与 device_info 对齐，最后回退模板。"""
        if self._user_agent:
            return self._user_agent
        from_device = user_agent_from_device_info(self._device_info)
        if from_device and "Headless" not in from_device:
            return from_device
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        )

    def _fresh_report(self) -> tuple[str, str]:
        """单次 hdaa 上报：刷新 Zf5 → 上报 → 解析 (cadd, sid)。"""
        assert self._device_info is not None
        payload_device = _refresh_zf5(self._device_info)
        try:
            body = _process_payload(payload_device, self._key)
        except Exception as e:
            raise SignError(f"组装签名载荷失败: {e}") from e

        url = self._report_url.format(uuid=str(uuid.uuid4()))
        headers = {
            "Content-Type": "application/octet-stream",
            "User-Agent": self._report_user_agent(),
            "Host": sign_conf.HDAA_HOST,
        }
        try:
            resp = requests.post(
                url, data=body, headers=headers,
                timeout=self._http_timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            raise SignError(f"设备指纹上报失败: {e}") from e

        try:
            decrypted = _aes_decrypt(base64.b64decode(resp.text), self._key)
            obj = json.loads(decrypted)
        except Exception as e:
            raise SignError(f"解析上报响应失败: {e}") from e

        cadd = str(obj.get("cadd") or "")
        sid = str(obj.get("sid") or "")
        if not cadd or not sid:
            raise SignError(f"上报响应缺少 cadd/sid: {obj}")
        return cadd, sid

    # ---- 调试辅助 ----
    def invalidate_cache(self) -> None:
        """兼容旧调用方；当前签名响应不缓存，因此无需执行任何操作。"""

    def device_info(self) -> dict:
        if self._device_info is None:
            self.open()
        assert self._device_info is not None
        return copy.deepcopy(self._device_info)
