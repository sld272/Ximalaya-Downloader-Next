# -*- coding: utf-8 -*-
"""HttpSource 契约测试：验证用 SignProvider + Cookie 走 baseInfo 的核心流程。

离线：
  - 不真正启动 Chrome 提取 cookie；
  - 不真正调用 hdaa report；
  - 不真正调用喜马拉雅 baseInfo。

通过替身注入与 monkeypatch 完成端到端验证。
"""
import base64
import json

import pytest

from xdl.adapters.source_http import HttpSource
from xdl.config import sign as sign_conf
from xdl.domain import Track, PlayUrl
from xdl.errors import RiskControlError, AuthError


class FakeSignProvider:
    """替身 SignProvider，配合 SignProvider Protocol。"""
    def __init__(self, value: str = "cadd_fake&&sid_fake"):
        self.value = value
        self.opened = False
        self.closed = False
        self.open_count = 0
        self.close_count = 0
        self.reload_count = 0
        self._device = {
            "HW5": "hw-original",
            "GJ2": "gj-original",
            "DP5": "dp-original",
            "adi": "adi-original",
            "acd": "acd-original",
            "ew1": {"yV2": "Mozilla/5.0"},
            "fd2": {},
            "Zf5": 0,
        }

    def open(self) -> None:
        self.opened = True
        self.open_count += 1

    def close(self) -> None:
        self.closed = True
        self.close_count += 1

    def sign(self) -> str:
        return self.value

    def reload(self, device_info=None) -> None:
        self.reload_count += 1
        if device_info is not None:
            self._device = dict(device_info)

    def device_info(self) -> dict:
        return dict(self._device)


class FakeDecoder:
    """复刻 Www2Decoder 接口的最小替身（不解真音频，原样回传）。"""
    def decode(self, encrypted_url: str) -> str:
        return encrypted_url + ".dec"


def _make_http_source(monkeypatch, sign_value="cadd_xyz&&sid_abc",
                      base_info_body=None, cookies=None,
                      cached_cookies=None,
                      chrome_path="/nonexist/chrome",
                      experiment_rotate_device_on_risk=False,
                      experiment_strip_device_cookies=True,
                      experiment_max_rotations=0,
                      experiment_persist_device_info=False,
                      response_bodies=None):
    """构造一个已经 ready 的 HttpSource（cookies 已加载、跳过浏览器提取）。"""
    cookies = cookies if cookies is not None else [
        {"name": "1&_token", "value": "tok", "domain": ".ximalaya.com",
         "path": "/"},
        {"name": "_xmLog", "value": "dev", "domain": ".ximalaya.com",
         "path": "/"},
    ]
    cached_cookies = cookies if cached_cookies is None else cached_cookies

    # 阻止任何真实网络/浏览器调用（注意：HttpSource 用 `from` 把名字带进了
    # 自己的命名空间，所以 patch 必须落在 `source_http` 模块上才生效）
    import xdl.adapters.source_http as mod
    monkeypatch.setattr(mod, "extract_cookies_from_profile",
                        lambda *a, **kw: cookies)
    monkeypatch.setattr(mod, "load_cached_cookies",
                        lambda *a, **kw: cached_cookies)
    monkeypatch.setattr(mod, "save_cookies", lambda *a, **kw: None)

    src = HttpSource(
        decoder=FakeDecoder(),
        sign_provider=FakeSignProvider(sign_value),
        chrome_path=chrome_path,
        profile_dir="/fake/profile",
        cookies_cache_path="/fake/cookies.json",
        resolve_timeout=5,
        chrome_headless=True,
        risk_recorder=None,
        impersonate="",  # 测试不需要 curl-cffi；_http_get 被 monkeypatch 拦截
        experiment_rotate_device_on_risk=experiment_rotate_device_on_risk,
        experiment_strip_device_cookies=experiment_strip_device_cookies,
        experiment_max_rotations=experiment_max_rotations,
        experiment_persist_device_info=experiment_persist_device_info,
        device_info_path="/fake/device-info.json",
    )

    # 替换 requests.get（baseInfo 调用）返回我们给的 body
    responses = []
    if response_bodies is not None:
        responses.extend(response_bodies)
    elif base_info_body is not None:
        responses.append(base_info_body)

    class _FakeResp:
        def __init__(self, body):
            self._body = body
            self.status_code = 200
            self.text = json.dumps(body)

        def json(self):
            return self._body

        def raise_for_status(self):
            pass

    def fake_get(url, params=None, headers=None, timeout=None):
        assert "xm-sign" in headers, "baseInfo 请求缺少 xm-sign 头"
        assert headers["xm-sign"] == sign_value
        assert headers["Cookie"], "baseInfo 请求缺少 Cookie 头"
        assert str(params.get("trackId"))
        # 验证 v3 端点路径（不再用 /revision/track/v1/baseInfo）
        assert "/mobile-playpage/track/v3/baseInfo/" in url, f"url={url}"
        if not responses:
            raise AssertionError("Unexpected extra baseInfo call")
        return _FakeResp(responses.pop(0))

    # patch HttpSource._http_get（统一拦下两层：curl-cffi 与 requests）
    monkeypatch.setattr(mod.HttpSource, "_http_get", lambda self, u, p, h: fake_get(u, p, h))

    # 让 asyncio 直接打开：绕过 chrome profile 检测
    import os
    monkeypatch.setattr(mod.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(mod.os.path, "exists", lambda p: True)

    return src


def _run_async(coro):
    import asyncio
    return asyncio.run(coro)


def test_http_source_open_loads_cookies_and_auth_state(monkeypatch):
    src = _make_http_source(monkeypatch)
    _run_async(src.open())
    assert src._cookie_header == "1&_token=tok; _xmLog=dev"
    assert src._authenticated is True


def test_http_source_can_use_valid_cache_without_profile(monkeypatch):
    """已有有效 Cookie 缓存时，不应强迫用户保留或重新启动 Chrome Profile。"""
    import xdl.adapters.source_http as mod

    cached = [{"name": "1&_token", "value": "tok",
               "domain": ".ximalaya.com", "path": "/"}]
    monkeypatch.setattr(mod, "load_cached_cookies", lambda *a, **kw: cached)
    monkeypatch.setattr(mod.os.path, "isdir", lambda _path: False)
    monkeypatch.setattr(
        mod, "extract_cookies_from_profile",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应启动 Chrome")),
    )
    signer = FakeSignProvider()
    src = HttpSource(
        FakeDecoder(), signer, profile_dir="/missing/profile",
        cookies_cache_path="/cached/cookies.json", impersonate="",
    )

    _run_async(src.open())

    assert src._authenticated is True
    assert signer.open_count == 1


def test_http_source_reopens_signer_after_close(monkeypatch):
    src = _make_http_source(monkeypatch)
    signer = src._sign

    _run_async(src.open())
    _run_async(src.close())
    _run_async(src.open())

    assert signer.open_count == 2
    assert signer.close_count == 1


def test_http_source_open_reextracts_when_cached_cookies_are_anonymous(monkeypatch):
    """匿名缓存不能遮蔽 Profile 中刚登录的会话。"""
    authenticated = [{"name": "1&_token", "value": "fresh",
                      "domain": ".ximalaya.com", "path": "/"}]
    anonymous = [{"name": "_xmLog", "value": "stale",
                  "domain": ".ximalaya.com", "path": "/"}]
    src = _make_http_source(
        monkeypatch, cookies=authenticated, cached_cookies=anonymous,
    )

    _run_async(src.open())

    assert src._authenticated is True
    assert src._cookie_header == "1&_token=fresh"


def test_cookie_extraction_does_not_navigate_by_default(tmp_path, monkeypatch):
    """导出已落盘 Cookie 不应额外访问站点或修改页面状态。"""
    import playwright.sync_api as sync_api
    from xdl.adapters.sign.cookies import extract_cookies_from_profile

    profile = tmp_path / "profile"
    profile.mkdir()

    class _Context:
        new_page_called = False
        closed = False

        def new_page(self):
            self.new_page_called = True
            raise AssertionError("默认导出不应创建导航页面")

        def cookies(self):
            return [{"name": "1&_token", "value": "persisted",
                     "domain": ".ximalaya.com", "path": "/"}]

        def close(self):
            self.closed = True

    context = _Context()

    class _Chromium:
        def launch_persistent_context(self, **_kwargs):
            return context

    class _Playwright:
        chromium = _Chromium()

    class _Manager:
        def __enter__(self):
            return _Playwright()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _Manager())

    cookies = extract_cookies_from_profile(str(profile), chrome_path="chrome")

    assert [c["name"] for c in cookies] == ["1&_token"]
    assert context.new_page_called is False
    assert context.closed is True


def test_save_cookies_keeps_existing_cache_when_write_fails(tmp_path, monkeypatch):
    """新缓存落盘失败时，不能破坏已有的有效缓存。"""
    import xdl.adapters.sign.cookies as mod

    path = tmp_path / "cookies.json"
    original = '[{"name":"1&_token","value":"old"}]'
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(mod.json, "dump",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        mod.save_cookies(
            [{"name": "1&_token", "value": "new", "domain": ".ximalaya.com"}],
            str(path),
        )

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".cookies-*.tmp")) == []


def test_http_source_get_track_success(monkeypatch):
    # v3 baseInfo 用 ret=0 表成功（与 getTracksList 的 ret=200 不同）。
    # trackInfo 直接挂在 body 上（不在 data 内），与 baseInfo v3 真实结构一致。
    base_body = {
        "ret": 0,
        "trackInfo": {
            "title": "示例曲目",
            "isPaid": True,
            "isAuthorized": False,
            "playUrlList": [
                {"type": "M4A_64", "url": "enc://audio1",
                 "fileSize": 12345},
                {"type": "MP3_32", "url": "enc://audio2",
                 "fileSize": 5432},
            ],
        },
    }
    src = _make_http_source(monkeypatch, base_info_body=base_body)
    _run_async(src.open())
    track = _run_async(src.get_track("852566950"))
    assert isinstance(track, Track)
    assert track.title == "示例曲目"
    assert track.is_paid is True
    # 解码器替身在 URL 末尾追加 .dec
    assert [p.url for p in track.play_urls] == [
        "enc://audio1.dec", "enc://audio2.dec",
    ]
    assert [p.type for p in track.play_urls] == ["M4A_64", "MP3_32"]


def test_http_source_get_track_risk_control_1001(monkeypatch):
    body = {"ret": 1001, "msg": "系统繁忙", "data": {}}
    src = _make_http_source(monkeypatch, base_info_body=body)
    _run_async(src.open())
    with pytest.raises(RiskControlError):
        _run_async(src.get_track("852566950"))


def test_http_source_get_track_anonymous_1001_is_api_error_not_risk(monkeypatch):
    """匿名态 + 1001 是匿名访问被拒，不是真风控——不应熔断整批。

    回归用：之前误把 v3 baseInfo 成功的 ret=0 当成失败 -> usecase 重试又重试，
    第 3 次撞真 1001。修正后 ret=0 直接成功，不会再触发这条路径；但已登录态 1001
    仍是真风控、匿名态 1001 退到 ApiError，这条边界必须保住。
    """
    body = {"ret": 1001, "msg": "系统繁忙，请稍后再试!"}
    # 匿名 Cookie：没有 1&_token
    cookies = [{"name": "_xmLog", "value": "dev",
                "domain": ".ximalaya.com", "path": "/"}]
    src = _make_http_source(monkeypatch, base_info_body=body, cookies=cookies)
    _run_async(src.open())
    assert src._authenticated is False
    from xdl.errors import ApiError
    with pytest.raises(ApiError):  # 不应是 RiskControlError
        _run_async(src.get_track("852566950"))


def test_http_source_get_track_url_uses_v3_endpoint(monkeypatch):
    """baseInfo 请求应打 v3 端点（路径里带时间戳），不是旧 v1。"""
    captured = {}
    base_body = {"ret": 0, "trackInfo": {"title": "x", "playUrlList": []}}
    src = _make_http_source(monkeypatch, base_info_body=base_body)

    # 用直接的 _http_get 拦截，记录 url
    def fake_get(self, url, params, headers):
        captured["url"] = url
        captured["params"] = params
        class _R:
            status_code = 200
            text = '{"ret":0,"trackInfo":{"title":"x","playUrlList":[]}}'
            def json(self): return {"ret": 0, "trackInfo": {"title": "x", "playUrlList": []}}
            def raise_for_status(self): pass
        return _R()
    monkeypatch.setattr(type(src), "_http_get", fake_get)

    _run_async(src.open())
    # play_url_list 为空会抛 ApiError，但 url 已经发出去了——我们只关心 URL 形态
    try:
        _run_async(src.get_track("852566950"))
    except Exception:
        pass
    assert "/mobile-playpage/track/v3/baseInfo/" in captured["url"]
    # 路径里有毫秒时间戳
    import re
    m = re.search(r"/mobile-playpage/track/v3/baseInfo/(\d+)$", captured["url"])
    assert m and len(m.group(1)) >= 13
    assert captured["params"] == {"device": "www2", "trackId": "852566950",
                                  "trackQualityLevel": "1"}


def test_http_source_get_track_3005_with_sysbusy_is_risk(monkeypatch):
    body = {"ret": 3005, "msg": "系统繁忙", "data": {}}
    src = _make_http_source(monkeypatch, base_info_body=body)
    _run_async(src.open())
    with pytest.raises(RiskControlError):
        _run_async(src.get_track("852566950"))


def test_http_source_get_track_3005_without_busy_is_auth(monkeypatch):
    body = {"ret": 3005, "msg": "无权访问", "data": {}}
    src = _make_http_source(monkeypatch, base_info_body=body)
    _run_async(src.open())
    with pytest.raises(AuthError):
        _run_async(src.get_track("852566950"))


def test_http_source_get_track_no_login_cookie_warns_but_proceeds(monkeypatch):
    body = {"ret": 200, "data": {"trackInfo": {
        "title": "免费", "playUrlList": [{"type": "MP3_64", "url": "enc"}]
    }}}
    cookies = [{"name": "_xmLog", "value": "dev",
                "domain": ".ximalaya.com", "path": "/"}]
    src = _make_http_source(monkeypatch, base_info_body=body, cookies=cookies)
    _run_async(src.open())
    assert src._authenticated is False
    track = _run_async(src.get_track("852566950"))
    assert track.title == "免费"


def test_get_album_uses_public_endpoint(monkeypatch):
    """get_album 走免签公开接口（HTTP 翻页），不依赖 xm-sign。"""
    src = _make_http_source(monkeypatch)
    _run_async(src.open())

    import xdl.adapters.source_http as mod
    from xdl.domain import Album, AlbumTrack

    def fake_fetch(album_id):
        return Album(
            album_id=album_id, title="专辑X", total=2,
            tracks=[
                AlbumTrack(track_id="1", title="t1", index=1),
                AlbumTrack(track_id="2", title="t2", index=2),
            ],
        )
    monkeypatch.setattr(mod, "_fetch_album_list", fake_fetch)

    album = _run_async(src.get_album("123"))
    assert album.title == "专辑X"
    assert len(album.tracks) == 2
    assert album.tracks[0].index == 1


def test_interactive_login_delegates_to_chrome_fallback(monkeypatch):
    """`xdl login` 在 http 后端下走 ChromeSource 完成登录，再顺带刷一次 Cookie 缓存。"""
    src = _make_http_source(monkeypatch)

    class _Fallback:
        def __init__(self):
            self.login_called = False
        def interactive_login(self):
            self.login_called = True
            return "/fake/profile"
    fb = _Fallback()
    src._chrome_fallback = fb

    # 让 HttpSource 在登录后顺带刷新 Cookie：替身掉真实的 Chrome 启动
    import xdl.adapters.source_http as mod
    cookies = [{"name": "1&_token", "value": "tok",
                "domain": ".ximalaya.com", "path": "/"}]
    monkeypatch.setattr(mod, "extract_cookies_from_profile",
                        lambda *a, **kw: cookies)
    saved = {}
    monkeypatch.setattr(mod, "save_cookies",
                        lambda c, p: saved.update({"c": c, "p": p}))

    path = src.interactive_login()
    assert fb.login_called
    assert path == "/fake/profile"
    assert src._cookie_header == "1&_token=tok"
    assert src._authenticated is True
    assert saved["c"] == cookies


def test_interactive_login_without_token_fails_without_overwriting_cache(monkeypatch):
    """Chrome 登录后的导出没有 token 时，不能伪报成功或覆盖旧缓存。"""
    src = _make_http_source(monkeypatch)

    class _Fallback:
        def interactive_login(self):
            return "/fake/profile"

    src._chrome_fallback = _Fallback()
    import xdl.adapters.source_http as mod
    anonymous = [{"name": "_xmLog", "value": "device",
                  "domain": ".ximalaya.com", "path": "/"}]
    monkeypatch.setattr(mod, "extract_cookies_from_profile",
                        lambda *a, **kw: anonymous)
    saved = []
    monkeypatch.setattr(mod, "save_cookies",
                        lambda *a, **kw: saved.append(a))

    with pytest.raises(AuthError):
        src.interactive_login()

    assert saved == []
    assert src._cookies == []


def test_refresh_cookies_without_token_keeps_current_authenticated_state(monkeypatch):
    """刷新失败不能把内存中的有效登录态降级为匿名态。"""
    src = _make_http_source(monkeypatch)
    src._cookies = [{"name": "1&_token", "value": "old",
                     "domain": ".ximalaya.com", "path": "/"}]
    src._cookie_header = "1&_token=old"
    src._authenticated = True

    import xdl.adapters.source_http as mod
    anonymous = [{"name": "_xmLog", "value": "device",
                  "domain": ".ximalaya.com", "path": "/"}]
    monkeypatch.setattr(mod, "extract_cookies_from_profile",
                        lambda *a, **kw: anonymous)
    saved = []
    monkeypatch.setattr(mod, "save_cookies",
                        lambda *a, **kw: saved.append(a))

    with pytest.raises(AuthError):
        _run_async(src.refresh_cookies())

    assert saved == []
    assert src._cookie_header == "1&_token=old"
    assert src._authenticated is True


def test_inspect_storage_delegates_to_chrome_fallback(monkeypatch):
    src = _make_http_source(monkeypatch)

    class _Fallback:
        async def inspect_storage(self):
            return {"diagnosis": "ok"}
    fb = _Fallback()
    src._chrome_fallback = fb

    report = _run_async(src.inspect_storage())
    assert report == {"diagnosis": "ok"}


def test_interactive_login_without_fallback_raises_config_error():
    """装配根没注入 chrome_fallback 时，login 给出清晰错误而不是 AttributeError。"""
    src = HttpSource(
        decoder=FakeDecoder(),
        sign_provider=FakeSignProvider(),
        profile_dir="/no/such",
        cookies_cache_path="",
        impersonate="",  # 跳过 curl-cffi 必装检查
    )
    import pytest
    with pytest.raises(Exception):  # ConfigError
        src.interactive_login()


def test_rotate_device_identity_reloads_from_browser(monkeypatch):
    """换身走真实浏览器提取结果。"""
    from xdl.adapters.sign.extractor import DeviceExtractResult

    src = _make_http_source(
        monkeypatch,
        experiment_strip_device_cookies=True,
    )
    _run_async(src.open())
    assert "_xmLog=dev" in src._cookie_header

    extracted = {
        "HW5": "hw-from-browser",
        "GJ2": "gj-from-browser",
        "DP5": "dp-from-browser",
        "adi": "adi-browser",
        "acd": "acd-browser",
        "ew1": {"yV2": "Mozilla/5.0"},
        "fd2": {"xz7": "uuid-browser"},
        "Zf5": 1,
    }
    browser_cookies = [
        {"name": "1&_token", "value": "tok", "domain": ".ximalaya.com", "path": "/"},
        {"name": "_xmLog", "value": "new-dev", "domain": ".ximalaya.com", "path": "/"},
        {"name": "other", "value": "1", "domain": ".ximalaya.com", "path": "/"},
    ]

    import xdl.adapters.source_http as mod
    monkeypatch.setattr(
        mod, "refresh_device_identity_via_browser",
        lambda **_kw: DeviceExtractResult(
            device_info=extracted,
            cookies=browser_cookies,
            cleared_cookie_names=["_xmLog", "wfp"],
            storage_report={"localStorageCleared": 3, "sessionStorageCleared": 1,
                            "indexedDB": ["treasure"]},
        ),
    )
    monkeypatch.setattr(mod, "save_device_info", lambda *_a, **_k: None)

    fp = _run_async(src.rotate_device_identity())

    assert fp
    assert src._sign.reload_count == 1
    assert src._sign.device_info()["HW5"] == "hw-from-browser"
    # strip 后浏览器导出的 _xmLog 不应进入 HTTP Cookie 头
    assert "_xmLog" not in src._cookie_header
    assert "1&_token=tok" in src._cookie_header
    assert src._device_fingerprint_was_reset is True
    assert src._device_rotations == 1


def _patch_browser_rotate(monkeypatch, *, hw: str = "hw-b"):
    from xdl.adapters.sign.extractor import DeviceExtractResult
    import xdl.adapters.source_http as mod

    monkeypatch.setattr(
        mod, "refresh_device_identity_via_browser",
        lambda **_kw: DeviceExtractResult(
            device_info={
                "HW5": hw, "GJ2": "gj-b", "DP5": "dp-b", "adi": "a",
                "acd": "c", "ew1": {"yV2": "ua"}, "fd2": {"xz7": "u"}, "Zf5": 1,
            },
            cookies=[{"name": "1&_token", "value": "tok",
                      "domain": ".ximalaya.com", "path": "/"}],
            cleared_cookie_names=["_xmLog"],
        ),
    )


def test_rotated_device_info_is_persisted_only_after_probe_success(monkeypatch):
    src = _make_http_source(
        monkeypatch,
        experiment_rotate_device_on_risk=True,
        experiment_persist_device_info=True,
    )
    _patch_browser_rotate(monkeypatch)
    saved = []
    import xdl.adapters.source_http as mod
    monkeypatch.setattr(
        mod, "save_device_info",
        lambda info, path: saved.append((info["HW5"], path)),
    )
    _run_async(src.open())

    assert _run_async(src._maybe_rotate_after_risk()) is True

    assert saved == []
    assert src._pending_device_info["HW5"] == "hw-b"

    _run_async(src._mark_post_rotate_success())

    assert saved == [("hw-b", "/fake/device-info.json")]
    assert src._pending_device_info is None


def test_immediate_risk_discards_unverified_device_info(monkeypatch):
    risk = {"ret": 1001, "msg": "系统繁忙", "data": {}}
    src = _make_http_source(
        monkeypatch,
        response_bodies=[risk, risk],
        experiment_rotate_device_on_risk=True,
        experiment_persist_device_info=True,
    )
    _patch_browser_rotate(monkeypatch)
    saved = []
    import xdl.adapters.source_http as mod
    monkeypatch.setattr(mod, "save_device_info", lambda *args: saved.append(args))
    _run_async(src.open())

    with pytest.raises(RiskControlError):
        _run_async(src.get_track("1"))

    assert saved == []
    assert src._pending_device_info is None
    assert src._rotate_disabled is True


def test_risk_control_rotates_then_retries(monkeypatch):
    risk = {"ret": 1001, "msg": "系统繁忙", "data": {}}
    ok = {
        "ret": 0,
        "trackInfo": {
            "title": "恢复曲",
            "playUrlList": [{"type": "MP3_64", "url": "enc://ok", "fileSize": 1}],
        },
    }
    src = _make_http_source(
        monkeypatch,
        response_bodies=[risk, ok],
        experiment_rotate_device_on_risk=True,
        experiment_max_rotations=0,
    )
    _patch_browser_rotate(monkeypatch)
    _run_async(src.open())
    track = _run_async(src.get_track("852566950"))
    assert track.title == "恢复曲"
    assert src._sign.reload_count == 1
    assert src._device_rotations == 1
    assert src._rotate_awaiting_success is False
    assert src._rotate_disabled is False


def test_risk_control_without_experiment_does_not_rotate(monkeypatch):
    body = {"ret": 1001, "msg": "系统繁忙", "data": {}}
    src = _make_http_source(monkeypatch, base_info_body=body)
    _run_async(src.open())
    with pytest.raises(RiskControlError):
        _run_async(src.get_track("852566950"))
    assert src._sign.reload_count == 0
    assert src._device_rotations == 0


def test_rotate_after_success_allows_another_rotation(monkeypatch):
    """换身后成功过，再遇风控仍可换身。"""
    risk = {"ret": 1001, "msg": "系统繁忙", "data": {}}
    ok = {
        "ret": 0,
        "trackInfo": {
            "title": "ok",
            "playUrlList": [{"type": "MP3_64", "url": "enc://ok", "fileSize": 1}],
        },
    }
    # 曲1: risk→rotate→ok；曲2: risk→rotate→ok
    src = _make_http_source(
        monkeypatch,
        response_bodies=[risk, ok, risk, ok],
        experiment_rotate_device_on_risk=True,
        experiment_max_rotations=0,
    )
    _patch_browser_rotate(monkeypatch)
    _run_async(src.open())
    assert _run_async(src.get_track("1")).title == "ok"
    assert src._device_rotations == 1
    assert _run_async(src.get_track("2")).title == "ok"
    assert src._device_rotations == 2
    assert src._rotate_disabled is False


def test_immediate_risk_after_rotate_disables_further_rotation(monkeypatch):
    """换身后首次请求仍风控 → 本会话不再换身。"""
    risk = {"ret": 1001, "msg": "系统繁忙", "data": {}}
    # 曲1: risk → rotate → risk(探针失败停用)；曲2: risk（不再 rotate）
    src = _make_http_source(
        monkeypatch,
        response_bodies=[risk, risk, risk],
        experiment_rotate_device_on_risk=True,
        experiment_max_rotations=0,
    )
    _patch_browser_rotate(monkeypatch)
    _run_async(src.open())
    with pytest.raises(RiskControlError):
        _run_async(src.get_track("1"))
    assert src._device_rotations == 1
    assert src._rotate_disabled is True
    # 下一曲也不应再换身
    with pytest.raises(RiskControlError):
        _run_async(src.get_track("2"))
    assert src._device_rotations == 1
    assert src._sign.reload_count == 1


def test_experiment_max_rotations_hard_cap(monkeypatch):
    risk = {"ret": 1001, "msg": "系统繁忙", "data": {}}
    ok = {
        "ret": 0,
        "trackInfo": {
            "title": "ok",
            "playUrlList": [{"type": "MP3_64", "url": "enc://ok", "fileSize": 1}],
        },
    }
    # 即便换身后成功，硬上限 1 也会阻止第二次换身
    src = _make_http_source(
        monkeypatch,
        response_bodies=[risk, ok, risk],
        experiment_rotate_device_on_risk=True,
        experiment_max_rotations=1,
    )
    _patch_browser_rotate(monkeypatch)
    _run_async(src.open())
    assert _run_async(src.get_track("1")).title == "ok"
    with pytest.raises(RiskControlError):
        _run_async(src.get_track("2"))
    assert src._device_rotations == 1
