import asyncio
import io
import json
import os
from pathlib import Path

import pytest
import requests

from xdl.adapters.apk import (ApkClient, ApkNativeBridge, ApkSource,
                              ApkStateStore)
from xdl.adapters.apk.client import _ret_error
from xdl.adapters.apk_sink import ApkMediaSink
from xdl.application.usecases import DownloadAlbumUseCase, RetryPolicy
from xdl.domain import Album, AlbumTrack, Quality
from xdl.errors import (AuthError, ConfigError, DownloadLimitError,
                        LoginRequiredError, SignError)
from xdl.settings import Settings
from xdl.application.facade import Facade
from xdl.frontends.web_runtime import WebRuntime


def run(value):
    return asyncio.run(value)


class FakeBridge:
    def __init__(self):
        self.tickets = []
        self.opened = 0
        self.encrypted_values = []

    def open(self):
        self.opened += 1

    def close(self):
        pass

    def create_xuid(self, stable_id):
        return "xuid-" + stable_id[-8:]

    def ticket(self, attr, xuid):
        self.tickets.append((attr, xuid))
        return "ticket-" + attr

    def decrypt_download(self, value, version):
        return "https://media.invalid/decrypted.m4a"

    def encrypt_mobile(self, value):
        self.encrypted_values.append(value)
        return f"encrypted-{len(self.encrypted_values)}"

    def sign(self, values):
        return "signature"


def _native_bridge(tmp_path, *, timeout=0.1):
    paths = {}
    for name in ("signer.jar", "libc++.so", "login.so", "xuid.so", "encrypt.so"):
        path = tmp_path / name
        path.write_bytes(b"test")
        paths[name] = str(path)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "na.czl").write_bytes(b"test")
    return ApkNativeBridge(
        java_path="java", signer_jar=paths["signer.jar"],
        libcxx=paths["libc++.so"], login_so=paths["login.so"],
        xuid_so=paths["xuid.so"], encrypt_so=paths["encrypt.so"],
        asset_dir=str(assets), timeout=timeout,
    )


def test_native_bridge_requires_download_key_asset(tmp_path):
    bridge = _native_bridge(tmp_path)

    with pytest.raises(ConfigError, match=r"drawable/x_m\.png"):
        bridge._validate()


def test_native_bridge_timeout_is_not_retried(monkeypatch, tmp_path):
    bridge = _native_bridge(tmp_path)
    calls = {"open": 0, "request": 0, "close": 0}

    def fake_open():
        calls["open"] += 1

    def fake_request(_payload):
        calls["request"] += 1
        raise SignError("APK native signer 响应超时（0.1s）。")

    def fake_close():
        calls["close"] += 1

    monkeypatch.setattr(bridge, "open", fake_open)
    monkeypatch.setattr(bridge, "_request_locked", fake_request)
    monkeypatch.setattr(bridge, "close", fake_close)

    with pytest.raises(SignError, match="响应超时"):
        bridge.call("decryptDownload", value="encrypted", version=1)

    assert calls == {"open": 1, "request": 1, "close": 1}


class Response:
    def __init__(self, value, status=200, headers=None, content=b""):
        self.value = value
        self.status_code = status
        self.headers = headers or {}
        self._content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(str(self.status_code))
            error.response = self
            raise error

    def json(self):
        return self.value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_content(self, chunk_size):
        del chunk_size
        yield self._content


class ProtocolSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if "/album/" in url:
            return Response({
                "ret": 0,
                "album": {"title": "APK Album"},
                "tracks": {"list": [
                    {"trackId": 1, "title": "One", "orderNo": 1,
                     "isAuthorized": True},
                    {"trackId": 2, "title": "Two", "orderNo": 2,
                     "isAuthorized": True},
                ], "pageId": 1, "maxPageId": 1, "totalCount": 2},
            })
        track_id = url.split("/track/")[1].split("/")[0]
        return Response({"ret": 0, "data": {
            "trackId": int(track_id), "title": f"Track {track_id}",
            "isAuthorized": True,
            "downloadAacUrl": f"https://media.invalid/{track_id}.m4a",
        }})

    def close(self):
        pass


class PasswordLoginSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if "/mobile/nonce/" in url:
            return Response({"ret": 0, "nonce": "nonce-value"})
        if url.endswith("mobile/login/pwd/v3"):
            return Response({"ret": 0, "data": {"uid": "100", "token": "token-value"}})
        raise AssertionError(f"unexpected request: {method} {url}")

    def close(self):
        pass


def captcha_payload(lot="lot-1"):
    return {
        "captcha_id": "3723312ce42a04b5c0b40e605a882037",
        "lot_number": lot,
        "pass_token": "pass",
        "gen_time": "123",
        "captcha_output": "output",
    }


@pytest.mark.parametrize(("mode", "account"), [
    ("mobile", "13800138000"),
    ("email", "listener@example.com"),
])
def test_apk_password_login_encrypts_credentials_and_persists_auth(
        tmp_path, mode, account):
    bridge = FakeBridge()
    session = PasswordLoginSession()
    state = ApkStateStore(str(tmp_path / mode))
    client = ApkClient(bridge, state, session=session, clock_ms=lambda: 123)

    result = client.login_password(account, "plain-password", mode, captcha_payload())

    assert result["authenticated"] is True
    assert state.load_auth() == ("100", "token-value")
    assert bridge.encrypted_values == [account, "plain-password"]
    request = next(call for call in session.calls if call[1].endswith("mobile/login/pwd/v3"))
    payload = request[2]["json"]
    assert request[0] == "POST"
    assert payload["account"] == "encrypted-1"
    assert payload["password"] == "encrypted-2"
    assert payload["nonce"] == "nonce-value"
    assert payload["signature"] == "signature"
    assert json.loads(payload["fdsOtp"])["lot_number"] == "lot-1"
    assert account not in payload.values()
    assert "plain-password" not in payload.values()


@pytest.mark.parametrize(("mode", "account", "message"), [
    ("mobile", "not-a-mobile", "手机号格式无效"),
    ("email", "not-an-email", "邮箱格式无效"),
])
def test_apk_password_login_rejects_invalid_account(tmp_path, mode, account, message):
    client = ApkClient(FakeBridge(), ApkStateStore(str(tmp_path / mode)),
                       session=PasswordLoginSession())

    with pytest.raises(Exception, match=message):
        client.login_password(account, "password", mode, captcha_payload())


def test_apk_password_login_requires_complete_fresh_captcha(tmp_path):
    client = ApkClient(FakeBridge(), ApkStateStore(str(tmp_path / "state")),
                       session=PasswordLoginSession())

    with pytest.raises(Exception, match="GeeTest 安全验证字段不完整"):
        client.login_password("13800138000", "password", "mobile", {})

    client.login_password("13800138000", "password", "mobile", captcha_payload())
    with pytest.raises(Exception, match="安全验证已经使用"):
        client.login_password("13800138000", "password", "mobile", captcha_payload())


class RecordingSink:
    def __init__(self):
        self.rows = []

    def write_track(self, url, track_id, quality, target_path, reporter,
                    cancel=None, progress_sink=None, expected_total=0):
        del reporter, cancel, progress_sink, expected_total
        self.rows.append((track_id, quality, url))
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        Path(target_path).write_bytes(b"audio")


def test_state_is_isolated_and_persistent(tmp_path):
    first = ApkStateStore(str(tmp_path / "apk"))
    first.save_auth("100", "secret")
    first.save_xuid("xuid")
    second = ApkStateStore(str(tmp_path / "apk"))

    assert second.device_id == first.device_id
    assert second.load_auth() == ("100", "secret")
    assert second.load_xuid() == "xuid"
    assert oct((tmp_path / "apk" / "accounts.json").stat().st_mode & 0o777) == "0o600"


def test_apk_state_migrates_legacy_auth_and_preserves_multiple_accounts(tmp_path):
    root = tmp_path / "apk"
    state = ApkStateStore(str(root))
    (root / "auth.json").write_text(json.dumps({
        "uid": "100", "token": "first", "savedAt": 1,
    }), encoding="utf-8")

    assert state.load_auth() == ("100", "first")
    assert not (root / "auth.json").exists()
    state.save_auth("200", "second")
    assert state.load_auth() == ("200", "second")
    assert {row["uid"] for row in state.list_accounts()} == {"100", "200"}

    assert state.switch_auth("100") == ("100", "first")
    assert state.delete_auth("100") == ("200", "second")
    assert state.list_accounts() == [{
        "uid": "200", "active": True,
        "saved_at": state.list_accounts()[0]["saved_at"],
    }]


def test_apk_client_switches_cookie_and_ticket_uid_without_changing_device(tmp_path):
    bridge = FakeBridge()
    state = ApkStateStore(str(tmp_path / "apk"))
    state.save_auth("100", "first")
    state.save_auth("200", "second")
    client = ApkClient(bridge, state, session=ProtocolSession())
    device_id = client.device_id

    assert "1&_token=200&second" in client._cookie()
    client.switch_account("100")
    assert client.device_id == device_id
    assert "1&_token=100&first" in client._cookie()
    client._ticket("download")
    assert "&u=100" in bridge.tickets[-1][0]
    accounts = client.auth_status()["accounts"]
    assert [row["uid"] for row in accounts] == ["100", "200"]
    assert accounts[0]["active"] is True


def test_apk_experience_download_limit_is_account_level_terminal_error():
    with pytest.raises(DownloadLimitError, match="体验会员下载上限"):
        _ret_error({"ret": 1, "msg": "已超过体验会员下载上限"}, "下载失败")


def test_apk_ret_726_remains_a_real_authorization_error():
    with pytest.raises(AuthError, match="暂无下载权限"):
        _ret_error({"ret": 726, "msg": "当前暂无下载权限"}, "下载失败")


def test_apk_is_authorized_false_with_valid_url_still_resolves(tmp_path):
    class FalseFlagSession(ProtocolSession):
        def request(self, method, url, **kwargs):
            if "/download/v2/track/" in url:
                self.calls.append((method, url, kwargs))
                return Response({"ret": 0, "data": {
                    "trackId": 42,
                    "title": "Downloadable",
                    "isAuthorized": False,
                    "downloadAacUrl": "encrypted-media-url",
                    "downloadEncryptVersion": 1,
                    "downloadQualityLevel": 1,
                    "highestQualityLevel": 1,
                }})
            return super().request(method, url, **kwargs)

    client = ApkClient(
        FakeBridge(), ApkStateStore(str(tmp_path / "state")),
        session=FalseFlagSession(), clock_ms=lambda: 123,
    )

    result = client.resolve_track("42", "high")

    assert result["url"] == "https://media.invalid/decrypted.m4a"
    assert result["raw"]["isAuthorized"] is False
    assert result["raw"]["downloadQualityLevel"] == 1


def test_apk_album_keeps_tracks_with_false_authorization_flag():
    class AlbumClient:
        def album_download_page(self, album_id, page, quality):
            assert (album_id, page, quality) == ("9", 1, 1)
            return {
                "album": {"title": "Album"},
                "tracks": [{
                    "trackId": 42, "title": "Downloadable", "orderNo": 1,
                    "isAuthorized": False,
                }],
                "pageId": 1, "maxPageId": 1, "totalCount": 1,
            }

    album = ApkSource(AlbumClient())._get_album_sync("9")

    assert [track.track_id for track in album.tracks] == ["42"]


def test_album_download_resolves_every_track_independently(tmp_path):
    bridge = FakeBridge()
    session = ProtocolSession()
    client = ApkClient(bridge, ApkStateStore(str(tmp_path / "state")),
                       session=session, clock_ms=lambda: 123)
    source = ApkSource(client)
    sink = RecordingSink()
    usecase = DownloadAlbumUseCase(
        source, sink, str(tmp_path / "downloads"), concurrency=1,
        retry=RetryPolicy(max_attempts=1, global_rounds=0),
    )

    result = run(usecase.execute("9", Quality.STANDARD))

    assert len(result.downloaded) == 2
    track_calls = [call for call in session.calls if "/download/v2/track/" in call[1]]
    assert [call[1].split("/track/")[1].split("/")[0] for call in track_calls] == ["1", "2"]
    assert [row[0] for row in sink.rows] == ["1", "2"]
    download_tickets = [attr for attr, _xuid in bridge.tickets if "s=download&" in attr]
    assert len(download_tickets) == 2


@pytest.mark.parametrize(("quality", "level"), [
    ("low", 0), ("standard", 1), ("high", 3),
])
def test_apk_quality_mapping_is_sent_once_per_track(tmp_path, quality, level):
    bridge = FakeBridge()
    session = ProtocolSession()
    client = ApkClient(bridge, ApkStateStore(str(tmp_path / quality)),
                       session=session, clock_ms=lambda: 123)

    client.resolve_track("42", quality)

    request = next(call for call in session.calls if "/download/v2/track/" in call[1])
    assert request[2]["params"]["trackQualityLevel"] == str(level)


class SinkSource:
    def __init__(self):
        self.calls = 0

    def resolve_track_sync(self, track_id, quality):
        from xdl.domain import PlayUrl, Track
        self.calls += 1
        return Track(track_id, "Track", [PlayUrl("M4A_64", "https://new.invalid/a.m4a")])


def test_apk_source_open_requires_login_before_starting_native_bridge():
    class LoggedOutClient:
        def __init__(self):
            self.opened = False

        def auth_status(self):
            return {"backend": "apk", "authenticated": False}

        def open(self):
            self.opened = True

    client = LoggedOutClient()

    with pytest.raises(LoginRequiredError, match="APK 协议尚未登录"):
        run(ApkSource(client).open())

    assert client.opened is False


def test_login_expiry_stops_album_batch_after_first_track(tmp_path):
    class ExpiredSource:
        def __init__(self):
            self.track_calls = 0

        async def open(self):
            pass

        async def close(self):
            pass

        async def get_album(self, album_id):
            return Album(album_id, "Album", total=3, tracks=[
                AlbumTrack(str(index), f"Track {index}", index)
                for index in range(1, 4)
            ])

        async def get_track(self, track_id):
            self.track_calls += 1
            raise LoginRequiredError("APK 登录态已失效")

    source = ExpiredSource()
    usecase = DownloadAlbumUseCase(
        source, RecordingSink(), str(tmp_path / "downloads"), concurrency=1,
        retry=RetryPolicy(max_attempts=1, global_rounds=0),
    )

    with pytest.raises(LoginRequiredError, match="登录态已失效"):
        run(usecase.execute("9", Quality.STANDARD))

    assert source.track_calls == 1


def test_apk_sink_refreshes_expired_url_only_once(monkeypatch, tmp_path):
    source = SinkSource()
    sink = ApkMediaSink(source)
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs["headers"]))
        if len(calls) == 1:
            return Response({}, status=404)
        return Response({}, status=200, headers={"Content-Length": "5"}, content=b"audio")

    monkeypatch.setattr(requests, "get", fake_get)
    target = tmp_path / "track.m4a"
    sink.write_track("https://old.invalid/a.m4a", "1", Quality.STANDARD,
                     str(target), None)

    assert target.read_bytes() == b"audio"
    assert source.calls == 1
    assert [row[0] for row in calls] == [
        "https://old.invalid/a.m4a", "https://new.invalid/a.m4a"
    ]
    assert all(row[1]["requestType"] == "download" for row in calls)


def test_apk_sink_resume_uses_current_fresh_url(monkeypatch, tmp_path):
    source = SinkSource()
    sink = ApkMediaSink(source)
    target = tmp_path / "track.m4a"
    part = Path(str(target) + ".part")
    part.write_bytes(b"old")
    Path(str(part) + ".meta").write_text('"etag"', encoding="utf-8")
    captured = {}

    def fake_get(url, **kwargs):
        captured.update(url=url, headers=kwargs["headers"])
        return Response({}, status=206,
                        headers={"Content-Range": "bytes 3-4/5"}, content=b"io")

    monkeypatch.setattr(requests, "get", fake_get)
    # 用例已在调用 write_track 前按本集重新解析；这里传入的就是当前新 URL。
    sink.write_track("https://fresh.invalid/a.m4a", "1", Quality.STANDARD,
                     str(target), None, expected_total=5)

    assert target.read_bytes() == b"oldio"
    assert captured["url"] == "https://fresh.invalid/a.m4a"
    assert captured["headers"]["Range"] == "bytes=3-"
    assert captured["headers"]["requestType"] == "download"


def test_apk_web_bootstrap_uses_apk_auth_not_browser_cache(tmp_path):
    class FacadeStub:
        def auth_status(self):
            return {"backend": "apk", "authenticated": True, "uid": "100"}

        def query_tasks(self, **_kwargs):
            from xdl.ports import TaskQueryResult
            from xdl.domain import TaskState
            return TaskQueryResult([], 0, {state: 0 for state in TaskState}, 0, 100)

    runtime = WebRuntime(
        Settings(source_backend="apk", task_db_path=str(tmp_path / "tasks.db")),
        facade=FacadeStub(), persist_settings=False,
    )

    login = runtime.bootstrap()["login"]

    assert login["authenticated"] is True
    assert login["browser_name"] == "APK"


def test_apk_facade_composition_is_isolated(tmp_path):
    app = Facade.from_config(Settings(
        source_backend="apk", apk_state_dir=str(tmp_path / "apk"),
    ))
    try:
        assert type(app._source).__name__ == "ApkSource"
        assert type(app._sink).__name__ == "ApkMediaSink"
    finally:
        app.close()
