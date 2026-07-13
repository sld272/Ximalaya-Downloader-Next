# -*- coding: utf-8 -*-
"""CLI 行为测试。"""
from types import SimpleNamespace

import pytest

from xdl.application.usecases import AlbumResult
from xdl.errors import AuthError
from xdl.frontends.cli import (_cmd_album, _cmd_refresh_cookies, _cmd_resume,
                               _cmd_risk_report)


class FakeApp:
    def __init__(self, album_result=None, resume_results=None):
        self.album_result = album_result
        self.resume_results = resume_results or []

    def download_album(self, target, quality=None, range_=None, reporter=None):
        return self.album_result

    def resume(self, reporter=None):
        return self.resume_results


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
