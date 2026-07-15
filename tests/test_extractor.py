# -*- coding: utf-8 -*-
"""设备指纹浏览器提取契约测试（不启动真实浏览器）。"""
import json
from types import SimpleNamespace

import pytest

from xdl.adapters.sign.extractor import (
    DeviceExtractResult,
    _clear_device_cookies_in_context,
    _remove_temp_profile,
    refresh_device_identity_via_browser,
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


class _ProfilePage:
    def __init__(self, *, fail_navigation=False):
        self.fail_navigation = fail_navigation

    def goto(self, *_args, **_kwargs):
        if self.fail_navigation:
            raise RuntimeError("navigation failed")

    def wait_for_timeout(self, _timeout):
        pass

    def evaluate(self, _script):
        return {"ok": True, "info": {"HW5": "fresh"}}


class _ProfileContext:
    def __init__(self, profile_dir, *, fail_navigation=False):
        self.page = _ProfilePage(fail_navigation=fail_navigation)
        self.closed = False
        (profile_dir / "profile-marker").write_text("x", encoding="utf-8")

    def new_page(self):
        return self.page

    def cookies(self):
        return []

    def close(self):
        self.closed = True


class _PlaywrightManager:
    def __init__(self, profile_dir, *, fail_navigation=False):
        self.profile_dir = profile_dir
        self.fail_navigation = fail_navigation
        self.context = None
        self.chromium = SimpleNamespace(
            launch_persistent_context=self._launch_persistent_context,
        )

    def _launch_persistent_context(self, **_kwargs):
        self.context = _ProfileContext(
            self.profile_dir, fail_navigation=self.fail_navigation,
        )
        return self.context

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _patch_temp_profile_runtime(monkeypatch, tmp_path, *, fail_navigation=False):
    profile_dir = tmp_path / "fresh-profile"

    def fake_mkdtemp(*, prefix):
        assert prefix == "xdl-device-"
        profile_dir.mkdir()
        return str(profile_dir)

    manager = _PlaywrightManager(
        profile_dir, fail_navigation=fail_navigation,
    )
    import playwright.sync_api as sync_api
    import xdl.adapters.sign.extractor as extractor
    monkeypatch.setattr(extractor.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: manager)
    return profile_dir, manager


def test_fresh_profile_is_removed_after_success(monkeypatch, tmp_path):
    profile_dir, manager = _patch_temp_profile_runtime(monkeypatch, tmp_path)

    result = refresh_device_identity_via_browser(
        fresh_profile=True,
        clear_device_state=False,
    )

    assert result.used_temp_profile is True
    assert result.profile_dir == str(profile_dir)
    assert manager.context.closed is True
    assert profile_dir.exists() is False


def test_fresh_profile_is_removed_after_browser_failure(monkeypatch, tmp_path):
    profile_dir, manager = _patch_temp_profile_runtime(
        monkeypatch, tmp_path, fail_navigation=True,
    )

    with pytest.raises(RuntimeError, match="navigation failed"):
        refresh_device_identity_via_browser(
            fresh_profile=True,
            clear_device_state=False,
        )

    assert manager.context.closed is True
    assert profile_dir.exists() is False


def test_temp_profile_cleanup_retries_transient_windows_lock(monkeypatch, tmp_path):
    profile_dir = tmp_path / "locked-profile"
    profile_dir.mkdir()
    (profile_dir / "marker").write_text("x", encoding="utf-8")
    import xdl.adapters.sign.extractor as extractor
    real_rmtree = extractor.shutil.rmtree
    calls = []
    delays = []

    def flaky_rmtree(path):
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError("still locked")
        real_rmtree(path)

    monkeypatch.setattr(extractor.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(extractor.time, "sleep", delays.append)

    assert _remove_temp_profile(str(profile_dir)) is True
    assert len(calls) == 3
    assert delays == [0.1, 0.2]
    assert profile_dir.exists() is False
