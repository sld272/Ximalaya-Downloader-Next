# -*- coding: utf-8 -*-
"""Facade 装配行为测试。"""
import os

import pytest

from xdl.application import Facade
from xdl.domain import Track, PlayUrl
from xdl.settings import Settings
from xdl.errors import ConfigError


class FakeSource:
    def __init__(self, track=None, error=None):
        self.opened = 0
        self.closed = 0
        self.track = track
        self.error = error

    def interactive_login(self):
        return "/tmp/xdl-profile"

    async def open(self):
        self.opened += 1

    async def close(self):
        self.closed += 1

    async def get_track(self, track_id):
        if self.error is not None:
            raise self.error
        return self.track or Track(
            track_id=track_id,
            title="曲目",
            play_urls=[PlayUrl("MP3_64", "http://x/a.mp3")],
        )


class FakeSink:
    def write(self, url, target_path, reporter, cancel=None, progress_sink=None,
              expected_total=0):
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, "wb") as f:
            f.write(b"x")


def test_login_does_not_construct_task_store(tmp_path):
    def fail_store():
        raise AssertionError("login 不应构造任务库")

    settings = Settings(download_dir=str(tmp_path))
    app = Facade(FakeSource(), FakeSink(), settings, store_factory=fail_store)

    assert app.login() == "/tmp/xdl-profile"


def test_track_tolerates_broken_store(tmp_path):
    from xdl.errors import StorageError

    def broken_store():
        raise StorageError("任务库损坏")

    settings = Settings(download_dir=str(tmp_path))
    app = Facade(FakeSource(), FakeSink(), settings, store_factory=broken_store)

    # 任务库坏了也不应挡住单曲下载（只是记不进面板）。
    path = app.download_track("1", quality="standard")
    assert path.endswith(".mp3")


def test_list_formats_returns_ranked_structured_formats_and_closes_source():
    source = FakeSource(Track("123", "曲目", [
        PlayUrl("MP3_32", "http://x/32.mp3", 1024),
        PlayUrl("M4A_64", "http://x/64.m4a", 2048),
        PlayUrl("MP3_128", "", 4096),
        PlayUrl("LOSSLESS", "http://x/lossless", 0),
    ]))
    app = Facade(source, FakeSink(), Settings(default_quality="high"))

    info = app.list_formats("https://www.ximalaya.com/sound/123")

    assert info == {
        "title": "曲目",
        "track_id": "123",
        "formats": [
            {"type": "M4A_64", "codec": "M4A", "bitrate": 64,
             "file_size": 2048},
            {"type": "MP3_32", "codec": "MP3", "bitrate": 32,
             "file_size": 1024},
            {"type": "LOSSLESS", "codec": "LOSSLESS", "bitrate": 0,
             "file_size": 0},
        ],
        "default_quality": "high",
    }
    assert (source.opened, source.closed) == (1, 1)


def test_list_formats_closes_source_when_track_lookup_fails():
    source = FakeSource(error=RuntimeError("boom"))
    app = Facade(source, FakeSink(), Settings())

    with pytest.raises(RuntimeError, match="boom"):
        app.list_formats("123")

    assert (source.opened, source.closed) == (1, 1)


def test_unknown_source_backend_fails_fast():
    with pytest.raises(ConfigError, match="未知音源后端"):
        Facade.from_config(Settings(source_backend="typo"))


def test_settings_rejects_non_positive_concurrency():
    with pytest.raises(ConfigError, match="并发数"):
        Settings(max_concurrency=0)


def test_close_releases_created_task_store():
    class Store:
        def __init__(self):
            self.closed = 0

        def all_tasks(self):
            return []

        def close(self):
            self.closed += 1

    store = Store()
    app = Facade(FakeSource(), FakeSink(), Settings(), store=store)

    app.close()
    app.close()

    assert store.closed == 1
