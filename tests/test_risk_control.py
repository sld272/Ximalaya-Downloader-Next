# -*- coding: utf-8 -*-
import asyncio
import builtins
import json

import pytest

from xdl.adapters.source_chrome import (ChromeSource, _has_login_cookie,
                                        _is_captcha_url, _parse_base_info_payload,
                                        _is_device_fingerprint_cookie,
                                        _partition_device_cookies)
from xdl.application.usecases import (DownloadTrackUseCase, DownloadAlbumUseCase,
                                      RetryPolicy)
from xdl.domain import Album, AlbumTrack, Quality
from xdl.errors import ApiError, AuthError, RiskControlError
from xdl.risk import RiskEventRecorder, summarize_risk_events


def run(coro):
    return asyncio.run(coro)


class _Decoder:
    def decode(self, value):
        return value


@pytest.mark.parametrize("ret,msg", [
    (1001, "系统繁忙"),
    (3005, "系统繁忙，请稍后再试!"),
])
def test_system_busy_responses_are_risk_control(ret, msg):
    source = ChromeSource(_Decoder(), "chrome", "profile")
    with pytest.raises(RiskControlError) as caught:
        source._raise_for({"ret": ret, "msg": msg})
    assert caught.value.ret == ret
    assert caught.value.retryable is True


def test_region_or_permission_response_stays_auth_error():
    source = ChromeSource(_Decoder(), "chrome", "profile")
    with pytest.raises(AuthError):
        source._raise_for({"ret": 927, "msg": "地区限制"})


def test_missing_web_token_stays_api_error_not_risk_control():
    source = ChromeSource(_Decoder(), "chrome", "profile")
    with pytest.raises(ApiError) as caught:
        source._raise_for({"ret": 407, "msg": "webtk缺失"})
    assert type(caught.value) is ApiError
    assert caught.value.ret == 407
    assert caught.value.retryable is False


def test_non_busy_3005_stays_auth_error():
    source = ChromeSource(_Decoder(), "chrome", "profile")
    with pytest.raises(AuthError):
        source._raise_for({"ret": 3005, "msg": "当前账号无权访问"})


def test_base_info_payload_parser_ignores_unrelated_tracks():
    body = {"ret": 0, "trackInfo": {"playUrlList": [{"url": "x"}]}}
    assert _parse_base_info_payload(
        "https://www.ximalaya.com/mobile-playpage/track/v3/baseInfo/1?trackId=999",
        body, "123",
    ) is None


def test_base_info_payload_parser_returns_target_error():
    assert _parse_base_info_payload(
        "https://www.ximalaya.com/mobile-playpage/track/v3/baseInfo/1?trackId=123",
        {"ret": 3005, "msg": "系统繁忙"}, "123",
    ) == (None, {"ret": 3005, "msg": "系统繁忙"})


class _TargetErrorPage:
    def __init__(self):
        self.listeners = {}
        self.evaluate_calls = 0

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        if self.listeners.get(event) is callback:
            del self.listeners[event]

    async def goto(self, *args, **kwargs):
        class Response:
            url = ("https://www.ximalaya.com/mobile-playpage/track/v3/"
                   "baseInfo/1?trackId=123")

            async def json(self):
                return {"ret": 1001, "msg": "系统繁忙"}

        self.listeners["response"](Response())

    async def evaluate(self, *args, **kwargs):
        self.evaluate_calls += 1


def test_capture_stops_as_soon_as_target_error_arrives():
    source = ChromeSource(_Decoder(), "chrome", "profile", resolve_timeout=1)
    source._ua_override = ""  # 有头/无需覆盖：跳过 UA 探测，聚焦捕获停止逻辑
    page = _TargetErrorPage()

    node, error = run(source._capture_base_info(page, "123"))

    assert node is None
    assert error == {"ret": 1001, "msg": "系统繁忙"}
    assert page.evaluate_calls == 0


def test_captcha_timeout_is_classified_as_risk_control():
    source = ChromeSource(_Decoder(), "chrome", "profile")
    with pytest.raises(RiskControlError) as caught:
        source._raise_for({"ret": None, "msg": None,
                           "timeout": True, "captcha": True})
    assert caught.value.retryable is True


def test_plain_timeout_without_captcha_is_retryable_api_error():
    source = ChromeSource(_Decoder(), "chrome", "profile")
    with pytest.raises(ApiError) as caught:
        source._raise_for({"ret": None, "msg": None,
                           "timeout": True, "captcha": False})
    assert type(caught.value) is ApiError
    assert caught.value.retryable is True
    # 不再是无信息的 ret=None/msg=None 文案
    assert "未捕获到目标 baseInfo" in str(caught.value)


def test_is_captcha_url_flags_geetest_but_not_lazy_module():
    assert _is_captcha_url("https://gcaptcha4.geetest.com/load?callback=x") is True
    assert _is_captcha_url("https://api.geetest.com/ajax.php") is True
    # 惰性预载的 fe-captcha 模块不应被当作已弹验证码
    assert _is_captcha_url(
        "https://s1.xmcdn.com/yx/fe-captcha/last/dist/index.js") is False


class _CaptchaTimeoutPage:
    """模拟风控惩罚态：目标 baseInfo 不返回，仅弹出 GeeTest 验证码资源。"""

    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        if self.listeners.get(event) is callback:
            del self.listeners[event]

    async def goto(self, *args, **kwargs):
        class CaptchaResponse:
            url = "https://gcaptcha4.geetest.com/load?callback=geetest_1"

            async def json(self):
                raise ValueError("not json")

        self.listeners["response"](CaptchaResponse())


def test_capture_reports_captcha_on_timeout():
    source = ChromeSource(_Decoder(), "chrome", "profile", resolve_timeout=1)
    page = _CaptchaTimeoutPage()

    node, error = run(source._capture_base_info(page, "123"))

    assert node is None
    assert error["timeout"] is True
    assert error["captcha"] is True


class _AutoplayNeededPage:
    """模拟：页面加载后不自动发 baseInfo，触发播放后才发出目标成功响应。"""

    def __init__(self):
        self.listeners = {}
        self.triggered = False

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        if self.listeners.get(event) is callback:
            del self.listeners[event]

    async def goto(self, *args, **kwargs):
        pass

    def locator(self, _selector):
        page = self

        class _Locator:
            @property
            def first(self):
                return self

            async def count(self):
                return 0

            async def click(self, **kwargs):
                page.triggered = True

        return _Locator()

    async def evaluate(self, script, *args):
        if "播放" in script or "querySelectorAll" in script:
            self.triggered = True

            class _Resp:
                url = ("https://www.ximalaya.com/mobile-playpage/track/v3/"
                       "baseInfo/1?trackId=123")

                async def json(self):
                    return {"ret": 0,
                            "trackInfo": {"playUrlList": [{"url": "enc"}]}}

            self.listeners["response"](_Resp())
        return None


def test_trigger_play_recovers_when_page_does_not_autofire():
    source = ChromeSource(_Decoder(), "chrome", "profile", resolve_timeout=1)
    source._ua_override = ""  # 跳过 UA 探测
    page = _AutoplayNeededPage()

    node, error = run(source._capture_base_info(page, "123"))

    assert page.triggered is True
    assert node is not None and node.get("playUrlList")
    assert error is None


_BASE_INFO_URL = ("https://www.ximalaya.com/mobile-playpage/track/v3/"
                  "baseInfo/1?trackId=123")


class _CaptchaThenSuccessPage:
    """惩罚态恢复：加载即回 3005 并弹验证码，稍后（模拟用户过完验证码、GeeTest 自动
    重发）自行回成功。恢复须忽略 3005、不反复戳页面，只被动等到成功。"""

    def __init__(self):
        self.listeners = {}
        self.play_triggers = 0

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        if self.listeners.get(event) is callback:
            del self.listeners[event]

    def _fire(self, ret, extra_url=""):
        listeners = self.listeners

        class _Resp:
            url = _BASE_INFO_URL if not extra_url else extra_url

            async def json(self):
                if ret == 0:
                    return {"ret": 0,
                            "trackInfo": {"playUrlList": [{"url": "enc"}]}}
                return {"ret": ret, "msg": "系统繁忙"}

        listeners["response"](_Resp())

    async def goto(self, *args, **kwargs):
        self._fire(3005)  # 过验证码前的 3005
        self._fire(0, "https://gcaptcha4.geetest.com/load?x=1")  # 弹验证码（活动信号）

        async def _later_success():
            await asyncio.sleep(2.0)   # 模拟用户过验证码后自动重发成功
            self._fire(0)

        asyncio.create_task(_later_success())

    def locator(self, _selector):
        page = self

        class _Locator:
            @property
            def first(self):
                return self

            async def count(self):
                return 0

            async def click(self, **kwargs):
                page.play_triggers += 1

        return _Locator()

    async def evaluate(self, script, *args):
        if "播放" in script or "querySelectorAll" in script:
            self.play_triggers += 1
        return None


def test_recovery_ignores_transient_busy_and_waits_for_success():
    source = ChromeSource(_Decoder(), "chrome", "profile")
    page = _CaptchaThenSuccessPage()

    node = run(source._await_success_through_captcha(page, "123", total_wait=30))

    assert node is not None and node.get("playUrlList")


def test_recovery_does_not_hammer_page_while_captcha_pending():
    # 页面已弹验证码（有活动）时，恢复期间绝不再触发播放，避免无限验证码。
    source = ChromeSource(_Decoder(), "chrome", "profile")
    page = _CaptchaThenSuccessPage()

    run(source._await_success_through_captcha(page, "123", total_wait=30))

    assert page.play_triggers == 0


def test_needs_captcha_fallback_only_for_captcha():
    from xdl.adapters.source_chrome import _needs_captcha_fallback
    assert _needs_captcha_fallback({"timeout": True, "captcha": True}) is True
    assert _needs_captcha_fallback({"timeout": True, "captcha": False}) is False
    assert _needs_captcha_fallback({"ret": 3005, "msg": "系统繁忙"}) is False
    assert _needs_captcha_fallback(None) is False


class _CdpSession:
    def __init__(self, sink):
        self._sink = sink

    async def send(self, method, params):
        self._sink[method] = params


class _UaProbePage:
    def __init__(self, ua):
        self._ua = ua
        self.overrides = {}

        class _Ctx:
            async def new_cdp_session(_self, _page):
                return _CdpSession(self.overrides)

        self.context = _Ctx()

    async def evaluate(self, _script, *args):
        return self._ua


def test_ua_override_strips_headless_token():
    source = ChromeSource(_Decoder(), "chrome", "profile")
    page = _UaProbePage(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) HeadlessChrome/150.0.0.0 Safari/537.36")

    run(source._apply_ua_override(page))

    override = page.overrides.get("Network.setUserAgentOverride")
    assert override is not None
    assert "HeadlessChrome" not in override["userAgent"]
    assert "Chrome/150" in override["userAgent"]
    assert source._ua_override and "HeadlessChrome" not in source._ua_override


def test_ua_override_noop_for_headful_ua():
    source = ChromeSource(_Decoder(), "chrome", "profile")
    page = _UaProbePage(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

    run(source._apply_ua_override(page))

    assert source._ua_override == ""
    assert page.overrides == {}


def test_login_cookie_detection_only_requires_token_name_and_value():
    assert _has_login_cookie([
        {"name": "1&_token", "value": "present"},
    ]) is True
    assert _has_login_cookie([
        {"name": "1&_device", "value": "device-only"},
        {"name": "wfp", "value": "analytics"},
    ]) is False


def test_interactive_login_reprompts_until_verified(tmp_path, monkeypatch):
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"")
    source = ChromeSource(_Decoder(), str(chrome), str(tmp_path / "profile"))
    launched = {}
    prompts = []
    verifications = iter([False, True])
    port_states = iter([False, True])

    class FakeProcess:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(args, **kwargs):
        launched["args"] = args
        return FakeProcess()

    def fake_input(prompt):
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("xdl.adapters.source_chrome.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "xdl.adapters.source_chrome._port_alive",
        lambda _port: next(port_states),
    )
    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(
        source, "_verify_interactive_login",
        lambda: next(verifications), raising=False,
    )

    assert source.interactive_login() == str(tmp_path / "profile")
    assert len(prompts) == 2
    assert "--remote-debugging-port=9222" in launched["args"]
    assert _has_login_cookie([
        {"name": "1&_token", "value": ""},
    ]) is False


class _RiskSource:
    def __init__(self):
        self.calls = []
        self.album = Album(
            "123", "专辑", total=3,
            tracks=[AlbumTrack(str(i), f"第{i}集", i) for i in range(1, 4)],
        )

    async def get_album(self, _album_id):
        return self.album

    async def get_track(self, track_id):
        self.calls.append(track_id)
        raise RiskControlError("系统繁忙", ret=3005)


class _Sink:
    def __init__(self):
        self.writes = []

    def write(self, *args, **kwargs):
        self.writes.append((args, kwargs))


def test_first_risk_control_opens_batch_circuit_breaker(tmp_path):
    source = _RiskSource()
    sink = _Sink()
    usecase = DownloadAlbumUseCase(
        source, sink, str(tmp_path), concurrency=1,
        retry=RetryPolicy(max_attempts=1, cooldown=0, global_rounds=2),
    )

    result = run(usecase.execute("123", Quality.STANDARD))

    assert source.calls == ["1"]
    assert sink.writes == []
    assert len(result.failed) == 3
    assert all("风控熔断" in message or "系统繁忙" in message
               for _track, message in result.failed)


def test_single_track_risk_control_is_not_immediately_retried(tmp_path):
    source = _RiskSource()
    usecase = DownloadTrackUseCase(
        source, _Sink(), str(tmp_path),
        retry=RetryPolicy(max_attempts=3, cooldown=0),
    )

    with pytest.raises(RiskControlError):
        run(usecase.execute("1", Quality.STANDARD))

    assert source.calls == ["1"]


def test_risk_event_recorder_writes_sanitized_jsonl(tmp_path):
    path = tmp_path / "risk-events.jsonl"
    recorder = RiskEventRecorder(str(path))

    recorder.record(
        track_id="400097657", elapsed_ms=1234, outcome="risk_control",
        ret=3005, msg="系统繁忙，请稍后再试!", in_flight=1,
        session_id="session-1", request_index=7,
        started_at="2026-07-11T00:00:00+00:00",
        authenticated=False,
    )

    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["track_id"] == "400097657"
    assert event["ret"] == 3005
    assert event["outcome"] == "risk_control"
    assert event["session_id"] == "session-1"
    assert event["request_index"] == 7
    assert event["started_at"] == "2026-07-11T00:00:00+00:00"
    assert event["authenticated"] is False
    assert "cookie" not in json.dumps(event).lower()
    assert "url" not in event


def test_risk_summary_reports_first_trigger_and_recovery(tmp_path):
    path = tmp_path / "risk-events.jsonl"
    rows = [
        {"timestamp": "2026-07-11T00:00:00+00:00", "track_id": "1",
         "elapsed_ms": 100, "outcome": "success", "ret": None,
         "msg": None, "in_flight": 1},
        {"timestamp": "2026-07-11T00:00:05+00:00", "track_id": "2",
         "elapsed_ms": 200, "outcome": "risk_control", "ret": 3005,
         "msg": "系统繁忙", "in_flight": 2},
        {"timestamp": "2026-07-11T00:02:05+00:00", "track_id": "3",
         "elapsed_ms": 150, "outcome": "success", "ret": None,
         "msg": None, "in_flight": 1},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                    encoding="utf-8")

    summary = summarize_risk_events(str(path))

    assert summary["total"] == 3
    assert summary["outcomes"] == {"success": 2, "risk_control": 1}
    assert summary["ret_counts"] == {"3005": 1}
    assert summary["first_risk_request_index"] == 2
    assert summary["successes_before_first_risk"] == 1
    assert summary["recovery_seconds"] == [120.0]
    assert summary["max_in_flight"] == 2
    assert summary["duration_seconds"] == 125.0
    assert summary["requests_per_minute"] == 1.44
    assert summary["peak_requests_per_minute"] == 2
    assert summary["request_interval_seconds"] == {
        "min": 5.0, "p50": 5.0, "p95": 5.0, "max": 120.0,
    }
    assert summary["outcomes_by_in_flight"] == {
        "1": {"success": 2},
        "2": {"risk_control": 1},
    }
    assert summary["outcomes_by_authentication"] == {
        "unknown": {"success": 2, "risk_control": 1},
    }


def test_risk_summary_uses_request_order_not_completion_order(tmp_path):
    path = tmp_path / "concurrent.jsonl"
    rows = [
        {"timestamp": "2026-07-11T00:00:03+00:00",
         "started_at": "2026-07-11T00:00:01+00:00", "session_id": "s",
         "request_index": 2, "track_id": "2", "elapsed_ms": 2000,
         "outcome": "risk_control", "ret": 3005, "in_flight": 2},
        {"timestamp": "2026-07-11T00:00:04+00:00",
         "started_at": "2026-07-11T00:00:00+00:00", "session_id": "s",
         "request_index": 1, "track_id": "1", "elapsed_ms": 4000,
         "outcome": "success", "ret": None, "in_flight": 1},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                    encoding="utf-8")

    summary = summarize_risk_events(str(path))

    assert summary["first_risk_request_index"] == 2
    assert summary["successes_before_first_risk"] == 1


def test_recovery_does_not_cross_authentication_state(tmp_path):
    path = tmp_path / "auth-change.jsonl"
    rows = [
        {"timestamp": "2026-07-11T00:00:00+00:00", "session_id": "old",
         "request_index": 1, "track_id": "1", "elapsed_ms": 100,
         "outcome": "risk_control", "ret": 1001, "in_flight": 1,
         "authenticated": False},
        {"timestamp": "2026-07-11T00:20:00+00:00", "session_id": "new",
         "request_index": 1, "track_id": "2", "elapsed_ms": 100,
         "outcome": "success", "ret": None, "in_flight": 1,
         "authenticated": True},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                    encoding="utf-8")

    summary = summarize_risk_events(str(path))

    assert summary["recovery_seconds"] == []
    assert summary["latest_session"] == {
        "session_id": "new",
        "authenticated": True,
        "total": 1,
        "outcomes": {"success": 1},
        "ret_counts": {},
        "first_risk_request_index": None,
        "successes_before_first_risk": 1,
        "request_interval_seconds": {},
        "max_in_flight": 1,
    }


def test_latest_session_reports_third_request_risk(tmp_path):
    path = tmp_path / "latest.jsonl"
    rows = [
        {"timestamp": "2026-07-11T00:00:01+00:00", "started_at": "2026-07-11T00:00:00+00:00",
         "session_id": "s", "request_index": 1, "track_id": "1", "elapsed_ms": 1000,
         "outcome": "success", "ret": None, "in_flight": 1, "authenticated": True},
        {"timestamp": "2026-07-11T00:00:10+00:00", "started_at": "2026-07-11T00:00:09+00:00",
         "session_id": "s", "request_index": 2, "track_id": "2", "elapsed_ms": 1000,
         "outcome": "success", "ret": None, "in_flight": 1, "authenticated": True},
        {"timestamp": "2026-07-11T00:00:19+00:00", "started_at": "2026-07-11T00:00:18+00:00",
         "session_id": "s", "request_index": 3, "track_id": "3", "elapsed_ms": 1000,
         "outcome": "risk_control", "ret": 3005, "in_flight": 1, "authenticated": True},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                    encoding="utf-8")

    latest = summarize_risk_events(str(path))["latest_session"]

    assert latest["first_risk_request_index"] == 3
    assert latest["successes_before_first_risk"] == 2
    assert latest["request_interval_seconds"] == {
        "min": 9.0, "p50": 9.0, "p95": 9.0, "max": 9.0,
    }


# ---- 设备指纹重置（保留登录态、清除 _xmLog/wfp/Hm_lvt_*） ----

def _cookie(name, **extra):
    base = {"name": name, "value": "v", "domain": ".ximalaya.com",
            "path": "/", "httpOnly": False, "secure": False,
            "sameSite": "Lax"}
    base.update(extra)
    return base


class _FakeStoragePage:
    """对应 _CLEAR_STORAGE_JS 求值的极简替身。"""

    def __init__(self, report=None):
        self.report = report if report is not None else {
            "localStorageCleared": 3, "sessionStorageCleared": 2,
            "indexedDB": ["treasure"],
        }
        self.closed = False

    async def goto(self, *_args, **_kwargs):
        return None

    async def evaluate(self, _script, *args):
        return self.report

    def evaluate_sync(self, _script, *args):
        return self.report

    def goto_sync(self, *_args, **_kwargs):
        return None

    async def close(self):
        self.closed = True

    def close_sync(self):
        self.closed = True


class _FakeCookieContext:
    """Playwright BrowserContext 的极简替身，实现 cookies/clear/add/new_page。"""

    def __init__(self, cookies, storage_report=None):
        self._cookies = list(cookies)
        self.cleared = False
        self.added = []
        self._storage_report = storage_report
        self.opened_pages: list[_FakeStoragePage] = []

    async def cookies(self, *_urls):
        return [dict(c) for c in self._cookies]

    async def clear_cookies(self):
        self.cleared = True
        self._cookies = []

    async def add_cookies(self, items):
        for c in items:
            self.added.append(dict(c))
            self._cookies.append(dict(c))

    async def new_page(self):
        page = _FakeStoragePage(self._storage_report)
        self.opened_pages.append(page)
        return page


class _FakeCookieContextSync:
    """同步版（供 _reset_device_cookies_sync 测试）。"""

    def __init__(self, cookies, storage_report=None):
        self._cookies = list(cookies)
        self.cleared = False
        self.added = []
        self._storage_report = storage_report
        self.opened_pages: list[_FakeStoragePage] = []

    def cookies(self, *_urls):
        return [dict(c) for c in self._cookies]

    def clear_cookies(self):
        self.cleared = True
        self._cookies = []

    def add_cookies(self, items):
        for c in items:
            self.added.append(dict(c))
            self._cookies.append(dict(c))

    def new_page(self):
        page = _FakeStoragePage(self._storage_report)
        page.evaluate = page.evaluate_sync
        page.goto = page.goto_sync
        page.close = page.close_sync
        self.opened_pages.append(page)
        return page


def test_is_device_fingerprint_cookie_matches_known_device_names():
    assert _is_device_fingerprint_cookie("_xmLog") is True
    assert _is_device_fingerprint_cookie("wfp") is True
    assert _is_device_fingerprint_cookie("Hm_lvt_4a7d8ec50cfd6af753c4f8aee3425070") is True
    assert _is_device_fingerprint_cookie("Hm_lpvt_4a7d8ec50cfd6af753c4f8aee3425070") is True


def test_is_device_fingerprint_cookie_keeps_login_cookies():
    assert _is_device_fingerprint_cookie("1&_token") is False
    assert _is_device_fingerprint_cookie("1&remember_me") is False
    assert _is_device_fingerprint_cookie("web_login") is False
    assert _is_device_fingerprint_cookie("tgw_l7_route") is False


def test_partition_separates_device_from_login():
    cookies = [
        _cookie("1&_token", httpOnly=True),
        _cookie("_xmLog"),
        _cookie("wfp"),
        _cookie("Hm_lvt_abc"),
        _cookie("web_login"),
    ]
    removed, kept = _partition_device_cookies(cookies)
    assert set(removed) == {"_xmLog", "wfp", "Hm_lvt_abc"}
    assert {c["name"] for c in kept} == {"1&_token", "web_login"}


def test_reset_device_cookies_drops_device_cookies_keeps_login():
    src = ChromeSource(_Decoder(), "chrome", "profile", reset_device_fingerprint=True)
    ctx = _FakeCookieContext([
        _cookie("1&_token", httpOnly=True),
        _cookie("1&remember_me"),
        _cookie("web_login"),
        _cookie("_xmLog"),
        _cookie("wfp"),
        _cookie("Hm_lvt_4a7d8ec50cfd6af753c4f8aee3425070"),
        _cookie("tgw_l7_route", domain="mobile.tx.ximalaya.com", secure=True),
    ])
    src._ctx = ctx

    run(src._reset_device_cookies())

    assert ctx.cleared is True
    names = {c["name"] for c in ctx._cookies}
    assert names == {"1&_token", "1&remember_me", "web_login", "tgw_l7_route"}
    assert "_xmLog" not in names and "wfp" not in names
    assert not any(n.startswith("Hm_lvt_") for n in names)
    assert src._device_fingerprint_was_reset is True


def test_reset_device_cookies_skipped_when_disabled():
    src = ChromeSource(_Decoder(), "chrome", "profile", reset_device_fingerprint=False)
    ctx = _FakeCookieContext([_cookie("_xmLog"), _cookie("1&_token")])
    src._ctx = ctx

    run(src._reset_device_cookies())

    assert ctx.cleared is False
    assert ctx._cookies and {c["name"] for c in ctx._cookies} == {"_xmLog", "1&_token"}
    assert src._device_fingerprint_was_reset is False


def test_reset_device_cookies_does_not_clear_when_no_device_cookies_but_storage_still_runs():
    src = ChromeSource(_Decoder(), "chrome", "profile", reset_device_fingerprint=True)
    ctx = _FakeCookieContext([_cookie("1&_token"), _cookie("web_login")])
    src._ctx = ctx

    run(src._reset_device_cookies())

    # 没有 Cookie 要删，所以不调 clear_cookies / add_cookies
    assert ctx.cleared is False
    # 但 storage 清理依旧执行：必须开页面清 localStorage / sessionStorage / IndexedDB
    assert len(ctx.opened_pages) == 1
    assert ctx.opened_pages[0].closed is True
    assert src._device_fingerprint_was_reset is True


def test_reset_device_cookies_clears_storage_after_cookies():
    storage_report = {"localStorageCleared": 3, "sessionStorageCleared": 2,
                      "indexedDB": ["treasure"]}
    src = ChromeSource(_Decoder(), "chrome", "profile", reset_device_fingerprint=True)
    ctx = _FakeCookieContext(
        [_cookie("_xmLog"), _cookie("1&_token"), _cookie("wfp")],
        storage_report=storage_report,
    )
    src._ctx = ctx

    run(src._reset_device_cookies())

    # 先清 Cookie
    assert ctx.cleared is True
    names = {c["name"] for c in ctx._cookies}
    assert names == {"1&_token"}
    assert "_xmLog" not in names and "wfp" not in names
    # 再开页面清 storage
    assert len(ctx.opened_pages) == 1
    assert ctx.opened_pages[0].closed is True
    assert src._device_fingerprint_was_reset is True


def test_reset_device_cookies_sync_clears_storage_after_cookies():
    storage_report = {"localStorageCleared": 3, "sessionStorageCleared": 2,
                      "indexedDB": ["treasure"]}
    src = ChromeSource(_Decoder(), "chrome", "profile", reset_device_fingerprint=True)
    ctx = _FakeCookieContextSync(
        [_cookie("_xmLog"), _cookie("1&_token"), _cookie("wfp")],
        storage_report=storage_report,
    )

    src._reset_device_cookies_sync(ctx)

    assert ctx.cleared is True
    names = {c["name"] for c in ctx._cookies}
    assert names == {"1&_token"}
    assert len(ctx.opened_pages) == 1
    assert ctx.opened_pages[0].closed is True
    assert src._device_fingerprint_was_reset is True


def test_reset_device_storage_runs_even_if_cookie_clear_fails():
    src = ChromeSource(_Decoder(), "chrome", "profile", reset_device_fingerprint=True)

    class _BadCookieCtx(_FakeCookieContext):
        async def cookies(self, *_urls):
            raise RuntimeError("cdp down")

    ctx = _BadCookieCtx([_cookie("irrelevant")])
    src._ctx = ctx

    run(src._reset_device_cookies())

    # Cookie 清失败不应阻断 storage 清理
    assert len(ctx.opened_pages) == 1
    assert ctx.opened_pages[0].closed is True
    assert src._device_fingerprint_was_reset is True


def test_reset_device_cookies_only_runs_once_per_session():
    src = ChromeSource(_Decoder(), "chrome", "profile", reset_device_fingerprint=True)
    ctx = _FakeCookieContext([_cookie("_xmLog"), _cookie("1&_token")])
    src._ctx = ctx

    run(src._reset_device_cookies())
    assert src._device_fingerprint_was_reset is True
    assert ctx.cleared is True
    first_cookies = list(ctx._cookies)

    # 再次调用不应重复清空（设备 Cookie 已不在；本会话只重置一次）
    run(src._reset_device_cookies())
    assert ctx._cookies == first_cookies


def test_reset_device_cookies_sync_drops_device_keeps_login():
    src = ChromeSource(_Decoder(), "chrome", "profile", reset_device_fingerprint=True)
    ctx = _FakeCookieContextSync([
        _cookie("1&_token", httpOnly=True),
        _cookie("_xmLog"),
        _cookie("wfp"),
        _cookie("web_login"),
    ])

    src._reset_device_cookies_sync(ctx)

    assert ctx.cleared is True
    names = {c["name"] for c in ctx._cookies}
    assert names == {"1&_token", "web_login"}
    assert src._device_fingerprint_was_reset is True


def test_reset_device_cookies_sync_skipped_when_disabled():
    src = ChromeSource(_Decoder(), "chrome", "profile", reset_device_fingerprint=False)
    ctx = _FakeCookieContextSync([_cookie("_xmLog"), _cookie("1&_token")])

    src._reset_device_cookies_sync(ctx)

    assert ctx.cleared is False
    assert src._device_fingerprint_was_reset is False


def test_risk_event_recorder_records_device_fingerprint_reset_flag(tmp_path):
    path = tmp_path / "reset.jsonl"
    recorder = RiskEventRecorder(str(path))

    recorder.record(
        track_id="1", elapsed_ms=100, outcome="success", in_flight=1,
        session_id="s", request_index=1,
        started_at="2026-07-11T00:00:00+00:00",
        authenticated=True, device_fingerprint_reset=True,
    )

    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["device_fingerprint_reset"] is True
    # 仍保留最小元数据原则：不含 Cookie 值 / 设备指纹内容
    serialized = json.dumps(event).lower()
    assert "cookie" not in serialized
    assert "value" not in event
