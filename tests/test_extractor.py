# -*- coding: utf-8 -*-
"""设备指纹浏览器提取契约测试（不启动真实浏览器）。"""
from xdl.adapters.sign.extractor import (
    DeviceExtractResult,
    summarize_extract,
)
from xdl.adapters.sign.cookies import is_login_cookie


def test_summarize_extract_includes_cleanup_and_login():
    result = DeviceExtractResult(
        device_info={"HW5": "x", "GJ2": "y"},
        cookies=[{"name": "1&_token", "value": "t"}],
        cleared_cookie_names=["_xmLog", "wfp"],
        storage_report={
            "localStorageCleared": 4,
            "sessionStorageCleared": 2,
            "indexedDB": ["treasure"],
        },
        used_temp_profile=False,
    )
    text = summarize_extract(result)
    assert "字段 2" in text
    assert "_xmLog" in text
    assert "localStorage=4" in text
    assert "treasure" in text
    assert "已登录" in text
    assert is_login_cookie(result.cookies)


def test_summarize_extract_marks_temp_profile_and_anonymous():
    result = DeviceExtractResult(
        device_info={"HW5": "x"},
        cookies=[{"name": "_xmLog", "value": "d"}],
        used_temp_profile=True,
    )
    text = summarize_extract(result)
    assert "无登录 token" in text
    assert "临时 Profile" in text
