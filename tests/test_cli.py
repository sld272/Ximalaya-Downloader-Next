# -*- coding: utf-8 -*-
"""CLI 行为测试。"""
from types import SimpleNamespace

from xdl.application.usecases import AlbumResult
from xdl.frontends.cli import _cmd_album, _cmd_resume


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
