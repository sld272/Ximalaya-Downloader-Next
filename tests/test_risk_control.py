# -*- coding: utf-8 -*-
import asyncio
import builtins
import json
import sqlite3

import pytest

from xdl.adapters.source_chrome import (ChromeSource, _has_login_cookie,
                                        _is_captcha_url, _parse_base_info_payload,
                                        _is_device_fingerprint_cookie)
from xdl.application.usecases import (DownloadTrackUseCase, DownloadAlbumUseCase,
                                      RetryPolicy)
from xdl.domain import Album, AlbumTrack, Quality
from xdl.errors import ApiError, AuthError, RiskControlError
from xdl.frontends.cli import _print_album_result
from xdl.risk import RiskEventRecorder, summarize_risk_events
from xdl.settings import Settings


def run(coro):
    return asyncio.run(coro)


class _Decoder:
    def decode(self, value):
        return value


def _patch_login_playwright(monkeypatch, cookies, pages=()):
    """替换同步 Playwright，返回可检查根 CDP 关闭请求的最小 browser。"""
    import playwright.sync_api as sync_api

    class _Context:
        def __init__(self):
            self.pages = list(pages)

        def cookies(self, *_urls):
            return list(cookies)

    class _Browser:
        def __init__(self):
            self.contexts = [_Context()]
            self.closed = False
            self.root_cdp_commands = []

        def close(self):
            self.closed = True

        def new_browser_cdp_session(self):
            browser = self

            class _RootSession:
                def send(self, method):
                    browser.root_cdp_commands.append(method)

            return _RootSession()

    browser = _Browser()

    class _Chromium:
        def connect_over_cdp(self, _url):
            return browser

    class _Playwright:
        chromium = _Chromium()

    class _Manager:
        def __enter__(self):
            return _Playwright()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _Manager())
    return browser


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


def test_persisted_login_cookie_check_reads_cookie_database_metadata_only(tmp_path):
    """登录成功必须能在关闭 Chrome 后从 Cookie DB 看到 token 条目。"""
    from xdl.adapters import source_chrome as mod

    db_dir = tmp_path / "Default" / "Network"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "Cookies"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE cookies (name TEXT, encrypted_value BLOB, value TEXT)"
        )
        conn.execute(
            "INSERT INTO cookies VALUES (?, ?, ?)",
            ("1&_token", b"encrypted-token", ""),
        )

    assert mod._has_persisted_login_cookie(str(tmp_path)) is True


def test_persisted_login_cookie_check_accepts_plaintext_cookie_storage(tmp_path):
    """兼容没有加密值、但仍有非空明文值的旧 Cookie DB。"""
    from xdl.adapters import source_chrome as mod

    db_dir = tmp_path / "Default" / "Network"
    db_dir.mkdir(parents=True)
    with sqlite3.connect(db_dir / "Cookies") as conn:
        conn.execute(
            "CREATE TABLE cookies (name TEXT, encrypted_value BLOB, value TEXT)"
        )
        conn.execute(
            "INSERT INTO cookies VALUES (?, ?, ?)",
            ("1&_token", b"", "plaintext-token"),
        )

    assert mod._has_persisted_login_cookie(str(tmp_path)) is True


def test_login_verification_rejects_dom_only_state(monkeypatch):
    """页面里泛化的 profile 链接不能替代 token Cookie。"""
    class _Page:
        url = "https://www.ximalaya.com/"

        def evaluate(self, _script):
            return {"logout": 0, "profileLinks": 1}

    browser = _patch_login_playwright(monkeypatch, [], pages=[_Page()])
    source = ChromeSource(_Decoder(), "chrome", "profile")

    assert source._verify_interactive_login() is False
    assert browser.closed is False


def test_login_verification_never_resets_profile_state(monkeypatch):
    """即使显式启用旧诊断开关，登录成功路径也不能修改 Profile。"""
    cookies = [
        {"name": "1&_token", "value": "present",
         "domain": ".ximalaya.com", "path": "/"},
        {"name": "tgw_l7_route", "value": "route",
         "domain": "mobile.ximalaya.com", "path": "/"},
        {"name": "foreign", "value": "ignore",
         "domain": ".example.com", "path": "/"},
    ]
    browser = _patch_login_playwright(
        monkeypatch, cookies,
    )
    source = ChromeSource(
        _Decoder(), "chrome", "profile", reset_device_fingerprint=True,
    )
    reset_calls = []
    monkeypatch.setattr(source, "_reset_device_cookies_sync",
                        lambda _context: reset_calls.append(True))

    assert source._verify_interactive_login() is True
    assert browser.root_cdp_commands == ["Browser.close"]
    assert browser.closed is False
    assert reset_calls == []
    assert [c["name"] for c in source.take_login_cookies()] == [
        "1&_token", "tgw_l7_route",
    ]
    assert source.take_login_cookies() == []


def test_device_fingerprint_reset_is_disabled_by_default():
    assert Settings().reset_device_fingerprint is False


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
    monkeypatch.setattr(
        "xdl.adapters.source_chrome._has_persisted_login_cookie",
        lambda _profile_dir: True, raising=False,
    )

    assert source.interactive_login() == str(tmp_path / "profile")
    assert len(prompts) == 2
    assert "--remote-debugging-port=9222" in launched["args"]
    assert _has_login_cookie([
        {"name": "1&_token", "value": ""},
    ]) is False


def test_interactive_login_rejects_unpersisted_token(tmp_path, monkeypatch):
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"")
    source = ChromeSource(_Decoder(), str(chrome), str(tmp_path / "profile"))

    class _Process:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    port_states = iter([False, True])
    monkeypatch.setattr("xdl.adapters.source_chrome.subprocess.Popen",
                        lambda *a, **kw: _Process())
    monkeypatch.setattr("xdl.adapters.source_chrome._port_alive",
                        lambda _port: next(port_states))
    monkeypatch.setattr(builtins, "input", lambda _prompt: "")
    monkeypatch.setattr(source, "_verify_interactive_login",
                        lambda: True, raising=False)
    monkeypatch.setattr(
        "xdl.adapters.source_chrome._has_persisted_login_cookie",
        lambda _profile_dir: False, raising=False,
    )

    with pytest.raises(AuthError, match="未持久化"):
        source.interactive_login()


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


class _PrintingReporter:
    def start(self, _title, _total):
        pass

    def update(self, _done, _total):
        pass

    def finish(self, _path):
        pass

    def note(self, message):
        print(message)


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
    assert result.failed == []
    assert result.deferred == 3
    assert "系统繁忙" in result.risk_control


def test_batch_risk_control_is_displayed_only_once(tmp_path, capsys):
    source = _RiskSource()
    usecase = DownloadAlbumUseCase(
        source, _Sink(), str(tmp_path), concurrency=1,
        retry=RetryPolicy(max_attempts=1, cooldown=0, global_rounds=2),
    )

    result = run(usecase.execute(
        "123", Quality.STANDARD, reporter=_PrintingReporter(),
    ))
    _print_album_result(result)

    output = capsys.readouterr().out
    risk_lines = [
        line for line in output.splitlines()
        if "风控" in line or "系统繁忙" in line
    ]
    assert len(risk_lines) == 1


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
        self.context = None  # 由 _FakeCookieContext.new_page 注入

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


class _FakeCdpSession:
    """极简 CDP session 替身：记录 Network.deleteCookie 调用，从 context 的 cookies
    里直接删掉对应条目，模拟 CDP 删特定 Cookie 的行为。"""

    def __init__(self, context):
        self._context = context
        self.enabled = False
        self.deleted_names: list[str] = []

    async def send(self, method, params=None):
        if method == "Network.enable":
            self.enabled = True
            return
        if method == "Network.deleteCookie":
            name = params.get("name", "")
            domain = params.get("domain", "")
            path = params.get("path", "/")
            self.deleted_names.append(name)
            self._context._cookies = [
                c for c in self._context._cookies
                if not (c.get("name") == name and
                        c.get("domain") == domain and
                        c.get("path", "/") == path)
            ]


class _FakeCdpSessionSync(_FakeCdpSession):
    def send(self, method, params=None):
        if method == "Network.enable":
            self.enabled = True
            return
        if method == "Network.deleteCookie":
            name = params.get("name", "")
            domain = params.get("domain", "")
            path = params.get("path", "/")
            self.deleted_names.append(name)
            self._context._cookies = [
                c for c in self._context._cookies
                if not (c.get("name") == name and
                        c.get("domain") == domain and
                        c.get("path", "/") == path)
            ]


class _FakeCookieContext:
    """Playwright BrowserContext 的极简替身，实现 cookies/new_page/new_cdp_session。"""

    def __init__(self, cookies, storage_report=None):
        self._cookies = list(cookies)
        self.cleared = False
        self.added = []
        self._storage_report = storage_report
        self.opened_pages: list[_FakeStoragePage] = []
        self.cdp_sessions: list[_FakeCdpSession] = []

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
        page.context = self
        self.opened_pages.append(page)
        return page

    async def new_cdp_session(self, _page):
        session = _FakeCdpSession(self)
        self.cdp_sessions.append(session)
        return session


class _FakeCookieContextSync:
    """同步版（供 _reset_device_cookies_sync 测试）。"""

    def __init__(self, cookies, storage_report=None):
        self._cookies = list(cookies)
        self.cleared = False
        self.added = []
        self._storage_report = storage_report
        self.opened_pages: list[_FakeStoragePage] = []
        self.cdp_sessions: list[_FakeCdpSessionSync] = []

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
        page.context = self
        self.opened_pages.append(page)
        return page

    def new_cdp_session(self, _page):
        session = _FakeCdpSessionSync(self)
        self.cdp_sessions.append(session)
        return session


def test_is_device_fingerprint_cookie_matches_known_device_names():
    assert _is_device_fingerprint_cookie("_xmLog") is True
    assert _is_device_fingerprint_cookie("wfp") is True
    assert _is_device_fingerprint_cookie("Hm_lvt_4a7d8ec50cfd6af753c4f8aee3425070") is True
    assert _is_device_fingerprint_cookie("Hm_lpvt_4a7d8ec50cfd6af753c4f8aee3425070") is True
    # 扩大后的设备/反垃圾载体
    assert _is_device_fingerprint_cookie("crystal") is True
    assert _is_device_fingerprint_cookie("HMACCOUNT") is True
    assert _is_device_fingerprint_cookie("cid") is True
    assert _is_device_fingerprint_cookie("_antispam_") is True
    assert _is_device_fingerprint_cookie("assva5") is True
    assert _is_device_fingerprint_cookie("assva6") is True
    assert _is_device_fingerprint_cookie("cmci9xde") is True
    assert _is_device_fingerprint_cookie("vmce9xdq") is True
    assert _is_device_fingerprint_cookie("pmck9xge") is True


def test_is_device_fingerprint_cookie_keeps_login_cookies():
    assert _is_device_fingerprint_cookie("1&_token") is False
    assert _is_device_fingerprint_cookie("1&remember_me") is False
    assert _is_device_fingerprint_cookie("web_login") is False
    assert _is_device_fingerprint_cookie("tgw_l7_route") is False
    assert _is_device_fingerprint_cookie("HWWAFSESID") is False
    assert _is_device_fingerprint_cookie("impl") is False


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

    # CDP deleteCookie 应被调用，没调 clear_cookies（保留登录态在磁盘上）
    assert len(ctx.cdp_sessions) >= 1
    session = ctx.cdp_sessions[0]
    assert session.enabled
    assert set(session.deleted_names) == {"_xmLog", "wfp", "Hm_lvt_4a7d8ec50cfd6af753c4f8aee3425070"}
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
    assert len(ctx.cdp_sessions) == 1
    assert set(ctx.cdp_sessions[0].deleted_names) == {"_xmLog", "wfp"}
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

    assert len(ctx.cdp_sessions) == 1
    assert set(ctx.cdp_sessions[0].deleted_names) == {"_xmLog", "wfp"}
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
    assert len(ctx.cdp_sessions) == 1
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

    assert len(ctx.cdp_sessions) == 1
    assert set(ctx.cdp_sessions[0].deleted_names) == {"_xmLog", "wfp"}
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
