# -*- coding: utf-8 -*-
"""媒体解码适配器（实现 Decoder 端口，见 docs/architecture.md §7.3）。

www2/mweb2 的音频 URL 解密：URL-safe Base64 → 还原 → S-Box 置换 → 双轮 XOR。
这是最易随平台变动的部分，故独立成适配器，并配「金标向量」测试回归。
"""
from __future__ import annotations

import base64

from ..config import platform
from ..errors import DecodeError


class Www2Decoder:
    """www2/mweb2 设备类型的 URL 解密。"""

    def __init__(self,
                 perm_table: list[int] | None = None,
                 xor_key: list[int] | None = None):
        self._perm = perm_table or platform.PERMUTATION_TABLE_O
        self._xor = xor_key or platform.XOR_KEY_A

    def decode(self, encrypted_url: str) -> str:
        if not encrypted_url:
            return ""
        # 已是明文直接返回
        if encrypted_url.startswith("http"):
            return encrypted_url

        cleaned = encrypted_url.replace("_", "/").replace("-", "+")
        cleaned += "=" * (-len(cleaned) % 4)   # 补齐 base64 padding（长度非 4 倍数会解码失败）
        try:
            decoded = base64.b64decode(cleaned)
        except Exception as e:
            raise DecodeError(f"Base64 解码失败: {e}") from e
        if len(decoded) < 16:
            raise DecodeError("密文长度不足（缺少 IV）")

        body = bytearray(decoded[:-16])
        iv = decoded[-16:]

        for i in range(len(body)):
            body[i] = self._perm[body[i]]

        for i in range(0, len(body), 16):
            for j in range(min(16, len(body) - i)):
                body[i + j] ^= iv[j]

        for i in range(0, len(body), 32):
            for j in range(min(32, len(body) - i)):
                body[i + j] ^= self._xor[j]

        return body.decode("utf-8", errors="replace")
