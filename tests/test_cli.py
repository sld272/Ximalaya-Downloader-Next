# -*- coding: utf-8 -*-
"""CLI 行为测试。"""
from types import SimpleNamespace

import pytest

from xdl.application.usecases import AlbumResult
from xdl.errors import AuthError
from xdl.frontends.cli import (_cmd_album, _cmd_refresh_cookies, _cmd_resume,
                               _cmd_risk_report, _cmd_track, _fmt_size,
                               build_parser, main)
from xdl.settings import Settings


class FakeApp:
    def __init__(self, album_result=None, resume_results=None, formats_info=None):
        self.album_result = album_result
        self.resume_results = resume_results or []
        self.formats_info = formats_info
        self.download_track_calls = 0

    def download_album(self, target, quality=None, range_=None, reporter=None):
        return self.album_result

    def resume(self, reporter=None):
        return self.resume_results

    def list_formats(self, target):
        return self.formats_info

    def download_track(self, target, quality=None, reporter=None):
        self.download_track_calls += 1
        return "downloaded.mp3"


def test_track_list_formats_uses_facade_result_without_downloading(capsys):
    app = FakeApp(formats_info={
        "title": "曲目",
        "track_id": "123",
        "default_quality": "standard",
        "formats": [
            {"type": "M4A_64", "codec": "M4A", "bitrate": 64,
             "file_size": 2 * 1024 * 1024},
            {"type": "LOSSLESS", "codec": "LOSSLESS", "bitrate": 0,
             "file_size": 0},
        ],
    })
    args = build_parser().parse_args(["track", "-F", "123"])

    assert _cmd_track(app, args) == 0

    output = capsys.readouterr().out
    assert app.download_track_calls == 0
    assert output.index("M4A_64") < output.index("LOSSLESS")
    assert "64k" in output
    assert "未知" in output


@pytest.mark.parametrize(("size", "expected"), [
    (0, "未知"),
    (512, "512 B"),
    (1024, "1.0 KB"),
    (1024 * 1024, "1.0 MB"),
])
def test_fmt_size(size, expected):
    assert _fmt_size(size) == expected


def test_album_stop_prints_summary_before_130(capsys):
    result = AlbumResult("专辑", downloaded=["a.mp3"], stopped=True)
    code = _cmd_album(
        FakeApp(album_result=result),
        SimpleNamespace(target="123", quality=None, range=None),
    )

    captured = capsys.readouterr()
    assert code == 130
    assert "专辑《专辑》：下载 1，跳过 0，失败 0。" in captured.out
    assert "已优雅停止" in captured.err


def test_resume_stop_prints_each_summary_before_130(capsys):
    result = AlbumResult("专辑", skipped=["a.mp3"], stopped=True)
    code = _cmd_resume(FakeApp(resume_results=[result]), SimpleNamespace())

    captured = capsys.readouterr()
    assert code == 130
    assert "专辑《专辑》：下载 0，跳过 1，失败 0。" in captured.out
    assert "已优雅停止" in captured.err


def test_album_batch_risk_returns_failure_and_prints_one_notice(capsys):
    result = AlbumResult(
        "专辑", risk_control="系统繁忙", deferred=3,
    )

    code = _cmd_album(
        FakeApp(album_result=result),
        SimpleNamespace(target="123", quality=None, range=None),
    )

    output = capsys.readouterr().out
    risk_lines = [
        line for line in output.splitlines()
        if "风控" in line or "系统繁忙" in line
    ]
    assert code == 1
    assert len(risk_lines) == 1


def test_resume_batch_risk_returns_failure(capsys):
    result = AlbumResult(
        "专辑", risk_control="系统繁忙", deferred=3,
    )

    code = _cmd_resume(
        FakeApp(resume_results=[result]), SimpleNamespace(),
    )

    assert code == 1
    assert capsys.readouterr().out.count("系统繁忙") == 1


def test_risk_report_is_local_only(tmp_path, capsys):
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"timestamp":"2026-07-11T00:00:00+00:00","track_id":"1",'
        '"elapsed_ms":10,"outcome":"risk_control","ret":3005,'
        '"msg":"系统繁忙","in_flight":1}\n',
        encoding="utf-8",
    )

    code = _cmd_risk_report(None, SimpleNamespace(log=str(path)))

    captured = capsys.readouterr()
    assert code == 0
    assert "总请求: 1" in captured.out
    assert "'3005': 1" in captured.out
    assert "平均请求速度(次/分钟)" in captured.out
    assert "并发分组" in captured.out
    assert "最新会话" in captured.out


def test_refresh_cookies_without_token_fails_without_overwriting_cache(monkeypatch):
    import xdl.adapters.sign as sign
    import xdl.frontends.cli as cli

    settings = SimpleNamespace(
        chrome_profile_dir="profile",
        chrome_path="chrome",
        cookies_cache_path="cookies.json",
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(sign, "extract_cookies_from_profile", lambda **_kw: [
        {"name": "_xmLog", "value": "anonymous"},
    ])
    saved = []
    monkeypatch.setattr(sign, "save_cookies", lambda *args: saved.append(args))

    with pytest.raises(AuthError, match="未发现登录 token"):
        _cmd_refresh_cookies(None, SimpleNamespace(no_headless=False))

    assert saved == []


def test_default_backend_is_local_xm_sign():
    assert Settings().source_backend == "http"


def test_experiment_rotate_defaults_off():
    settings = Settings()
    assert settings.experiment_rotate_device_on_risk is False
    assert settings.experiment_strip_device_cookies is True
    assert settings.experiment_max_device_rotations == 0


def test_main_enables_experiment_rotate_flag(monkeypatch):
    import xdl.frontends.cli as cli

    captured = {}

    def fake_from_config(settings):
        captured["settings"] = settings

        class _App:
            def download_album(self, *a, **k):
                from types import SimpleNamespace
                return SimpleNamespace(failed=[], stopped=False, summary=lambda: "ok")

        return _App()

    monkeypatch.setattr(cli.Facade, "from_config", staticmethod(fake_from_config))
    code = cli.main([
        "--experiment-rotate-device",
        "album", "123", "--range", "1-2",
    ])
    assert code == 0
    assert captured["settings"].experiment_rotate_device_on_risk is True



def test_main_help_focuses_on_normal_user_flow(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "{web,login,track,album,resume,gen-sign,risk-report}" in output
    assert "启动本地 WebUI" in output
    assert "extract-device" not in output
    assert "refresh-cookies" not in output
    assert "inspect" not in output


def test_gen_sign_repeat_must_be_positive(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["gen-sign", "-n", "0"])

    assert exc.value.code == 2
    assert "必须是大于 0 的整数" in capsys.readouterr().err


def test_concurrency_must_be_positive(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--concurrency", "0", "album", "123"])

    assert exc.value.code == 2
    assert "必须是大于 0 的整数" in capsys.readouterr().err


def test_web_port_must_be_valid(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["web", "--port", "0"])

    assert exc.value.code == 2
    assert "端口必须在 1 到 65535 之间" in capsys.readouterr().err


def test_main_passes_custom_concurrency_to_settings(monkeypatch):
    import xdl.frontends.cli as cli

    captured = {}
    app = FakeApp(album_result=AlbumResult("专辑"))

    def fake_from_config(settings):
        captured["settings"] = settings
        return app

    monkeypatch.setattr(cli.Facade, "from_config", fake_from_config)

    assert main(["--concurrency", "3", "album", "123"]) == 0
    assert captured["settings"].max_concurrency == 3


def test_local_risk_report_does_not_build_facade(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    path.write_text("", encoding="utf-8")

    import xdl.frontends.cli as cli
    monkeypatch.setattr(
        cli.Facade, "from_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("本地报告不应装配下载器")
        ),
    )

    assert main(["risk-report", "--log", str(path)]) == 0
