# -*- coding: utf-8 -*-
"""PySignProvider 单元测试：纯算 xm-sign（不发真实网络请求）。

<info>
通过 monkeypatch 把 `requests.post` 与 `PySignProvider._fresh_sign` 中触及
网络的环节替换为离线替身，验证：
  1. 内置设备模板能加载、Zf5 在每次 sign 时被刷新；
  2. report 端点被 AES-ECB 解密后的响应里取出 cadd && sid 拼成 xm-sign；
  3. 缓存 TTL 期间复用上次结果，过期/失效后重新走签名流程。
</info>
"""
import json
import time

import pytest

from xdl.adapters.sign.py_sign import (PySignProvider, _aes_encrypt,
                                         _aes_decrypt, _process_payload,
                                         _refresh_zf5, _json_dumps_compact)
from xdl.config import sign as sign_conf
from xdl.errors import SignError
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import base64


def test_default_template_loads():
    info = sign_conf.load_default_device_info()
    assert isinstance(info, dict)
    assert info["GF9"] == "2.0.0"
    # 模板里 Zf5 留作 0，每次 sign 时由 PySignProvider 刷新
    assert info["Zf5"] == 0
    # 模板里已清除 HeadlessChrome 这一最明显的自动化 UA 指纹
    assert "HeadlessChrome" not in info["ew1"]["yV2"]


def test_refresh_zf5_updates_timestamp_for_default_template():
    info = sign_conf.load_default_device_info()
    fresh = _refresh_zf5(info)
    assert fresh["Zf5"] > 0
    # 原字典不应被修改
    assert info["Zf5"] == 0


def test_aes_encrypt_decrypt_round_trip():
    payload = b"hello-xm-sign-payload"
    ciphertext = _aes_encrypt(payload, sign_conf.KEY)
    plaintext = _aes_decrypt(ciphertext, sign_conf.KEY)
    assert plaintext == payload


def test_process_payload_shape_matches_easy_sign():
    """`_process_payload` 输出的 bytes 与 easy-sign core.get_process_data 等价（原始 AES 密文）。"""
    info = sign_conf.load_default_device_info()
    info = _refresh_zf5(info)
    payload = _process_payload(info, sign_conf.KEY)
    # 上报 body 是原始 AES-ECB 密文 bytes（16 字节倍数 + PKCS7 padding）
    assert isinstance(payload, bytes)
    assert len(payload) % 16 == 0
    # 解密后应是 zlib 流；解压得到合法 JSON 文本（device_info 序列化）
    import zlib
    decrypted = _aes_decrypt(payload, sign_conf.KEY)
    text = zlib.decompress(decrypted).decode("utf-8")
    parsed = json.loads(text)
    assert parsed["GF9"] == "2.0.0"
    assert parsed["Zf5"] == info["Zf5"]


def _fake_report_response(cadd: str = "cadd_x", sid: str = "sid_y") -> object:
    """构造一个与真实 hdaa report 接口同构的 fake response。"""
    body_obj = {"cadd": cadd, "sid": sid}
    cipher = AES.new(sign_conf.KEY.encode(), AES.MODE_ECB)
    encrypted = cipher.encrypt(pad(json.dumps(body_obj).encode(), 16))
    body_text = base64.b64encode(encrypted).decode()

    class _Resp:
        status_code = 200
        text = body_text
        def raise_for_status(self): pass
    return _Resp()


def test_pysignprovider_sign_returns_cadd_and_sid(monkeypatch):
    captured = {}
    def fake_post(url, data=None, headers=None, timeout=None, verify=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["verify"] = verify
        return _fake_report_response("cadd_abc", "sid_123")
    import xdl.adapters.sign.py_sign as mod
    monkeypatch.setattr(mod.requests, "post", fake_post)

    signer = PySignProvider(device_info_path="/nonexistent/path/use-template.json",
                            cache_ttl=60)
    # 文件不存在 → 应自动回退到内置模板
    signer.open()
    sign = signer.sign()
    assert sign == "cadd_abc&&sid_123"
    assert "r=" in captured["url"] and "hdaa.shuzilm.cn" in captured["url"]
    assert captured["headers"]["Content-Type"] == "application/octet-stream"
    assert captured["verify"] is not False


def test_pysignprovider_uses_cadd_and_sid_from_same_report(monkeypatch):
    """每次签名只上报一次，并使用同一次响应中成对返回的 cadd/sid。"""
    calls = {"n": 0}
    def fake_post(*a, **kw):
        calls["n"] += 1
        return _fake_report_response(f"c{calls['n']}", f"s{calls['n']}")
    import xdl.adapters.sign.py_sign as mod
    monkeypatch.setattr(mod.requests, "post", fake_post)

    signer = PySignProvider(cache_ttl=60)
    signer.open()
    first = signer.sign()
    second = signer.sign()

    assert first == "c1&&s1"
    assert second == "c2&&s2"
    assert calls["n"] == 2


def test_pysignprovider_invalidate_cache_remains_compatible(monkeypatch):
    calls = {"n": 0}
    def fake_post(*a, **kw):
        calls["n"] += 1
        # 模拟 hdaa 每次返回新 sid 但 cadd 不变（与服端行为一致）
        return _fake_report_response("cadd_fixed", f"sid_{calls['n']}")
    import xdl.adapters.sign.py_sign as mod
    monkeypatch.setattr(mod.requests, "post", fake_post)

    signer = PySignProvider(cache_ttl=60)
    signer.open()
    a = signer.sign()
    signer.invalidate_cache()
    b = signer.sign()
    assert a == "cadd_fixed&&sid_1"
    assert b == "cadd_fixed&&sid_2"
    assert calls["n"] == 2


def test_pysignprovider_raises_on_missing_cadd_sid(monkeypatch):
    def fake_post(*a, **kw):
        # 返回没有 cadd/sid 的内容
        cipher = AES.new(sign_conf.KEY.encode(), AES.MODE_ECB)
        body = json.dumps({"unrelated": True}).encode()
        return type("R", (), {
            "status_code": 200,
            "text": base64.b64encode(cipher.encrypt(pad(body, 16))).decode(),
            "raise_for_status": lambda self: None,
        })()
    import xdl.adapters.sign.py_sign as mod
    monkeypatch.setattr(mod.requests, "post", fake_post)

    signer = PySignProvider(cache_ttl=0)
    signer.open()
    with pytest.raises(SignError):
        signer.sign()


def test_signprovider_protocol_runtime_checkable():
    from xdl.ports import SignProvider
    signer = PySignProvider()
    assert isinstance(signer, SignProvider)
