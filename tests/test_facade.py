# -*- coding: utf-8 -*-
"""Facade 装配行为测试。"""
import os

from xdl.application import Facade
from xdl.domain import Track, PlayUrl
from xdl.settings import Settings


class FakeSource:
    def __init__(self):
        self.opened = 0
        self.closed = 0

    def interactive_login(self):
        return "/tmp/xdl-profile"

    async def open(self):
        self.opened += 1

    async def close(self):
        self.closed += 1

    async def get_track(self, track_id):
        return Track(track_id=track_id, title="曲目",
                     play_urls=[PlayUrl("MP3_64", "http://x/a.mp3")])


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
