# -*- coding: utf-8 -*-
"""设备指纹浏览器提取契约测试（不启动真实浏览器）。"""
import json

from xdl.adapters.sign.extractor import (
    DeviceExtractResult,
    _clear_device_cookies_in_context,
    save_device_info,
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


class _CookieDeleteSession:
    def __init__(self, context, fail_delete=False):
        self.context = context
        self.fail_delete = fail_delete
        self.enabled = False
        self.detached = False

    def send(self, method, params=None):
        if method == "Network.enable":
            self.enabled = True
            return
        assert method == "Network.deleteCookie"
        if self.fail_delete:
            raise RuntimeError("delete failed")
        self.context.cookies_data = [
            cookie for cookie in self.context.cookies_data
            if not (
                cookie["name"] == params["name"]
                and cookie["domain"] == params["domain"]
                and cookie["path"] == params["path"]
            )
        ]

    def detach(self):
        self.detached = True


class _CookieDeleteContext:
    def __init__(self, *, fail_delete=False):
        self.cookies_data = [
            {
                "name": "1&_token", "value": "token",
                "domain": ".ximalaya.com", "path": "/",
            },
            {
                "name": "_xmLog", "value": "device",
                "domain": ".ximalaya.com", "path": "/",
            },
        ]
        self.session = _CookieDeleteSession(self, fail_delete=fail_delete)

    def cookies(self):
        return list(self.cookies_data)

    def new_cdp_session(self, _page):
        return self.session

    def clear_cookies(self):
        raise AssertionError("不得清空整个 Cookie 容器")


def test_clear_device_cookies_uses_targeted_cdp_delete_and_keeps_login():
    context = _CookieDeleteContext()

    removed = _clear_device_cookies_in_context(context, object())

    assert removed == ["_xmLog"]
    assert context.session.enabled is True
    assert context.session.detached is True
    assert [cookie["name"] for cookie in context.cookies_data] == ["1&_token"]


def test_clear_device_cookies_failure_never_clears_login_cookie():
    context = _CookieDeleteContext(fail_delete=True)

    removed = _clear_device_cookies_in_context(context, object())

    assert removed == []
    assert context.session.detached is True
    assert {cookie["name"] for cookie in context.cookies_data} == {
        "1&_token", "_xmLog",
    }


def test_save_device_info_atomically_replaces_existing_file(tmp_path):
    output = tmp_path / "device-info.json"
    output.write_text("old", encoding="utf-8")

    save_device_info({"HW5": "new"}, str(output))

    assert json.loads(output.read_text(encoding="utf-8")) == {"HW5": "new"}
    assert list(tmp_path.glob(".device-info-*.tmp")) == []
