# -*- coding: utf-8 -*-
import os
import threading

import pytest

from xdl.application.usecases import AlbumResult
from xdl.domain import DownloadTask, TaskState
from xdl.errors import CancelledByUser
from xdl.frontends.web_runtime import (OperationBusyError,
                                       WebRuntime)
from xdl.settings import Settings


class FakeFacade:
    def __init__(self, *, blocking=False):
        self.blocking = blocking
        self.started = threading.Event()
        self.closed = 0
        self.tasks = [
            DownloadTask(
                id=1, track_id="11", album_id="22", title="第一集",
                quality="standard", album_index=1,
                state=TaskState.DOWNLOADING, bytes_done=25, total_bytes=100,
            ),
            DownloadTask(
                id=2, track_id="12", album_id="22", title="第二集",
                quality="standard", album_index=2, state=TaskState.DONE,
                bytes_done=100, total_bytes=100,
            ),
        ]

    def all_tasks(self):
        return self.tasks

    def download_track(self, target, quality=None, reporter=None, cancel=None):
        self.started.set()
        reporter.start("测试单曲", 100)
        if self.blocking:
            cancel.wait(2)
            raise CancelledByUser("用户已停止")
        reporter.update(100, 100)
        reporter.finish("/tmp/test.mp3")
        return "/tmp/test.mp3"

    def download_album(self, target, quality=None, range_=None,
                       reporter=None, cancel=None):
        return AlbumResult("测试专辑", downloaded=["a.mp3"])

    def resume(self, reporter=None, cancel=None):
        return [AlbumResult("测试专辑", skipped=["a.mp3"])]

    def login(self):
        return "/tmp/profile"

    def list_formats(self, target):
        return {"track_id": target, "title": "测试单曲", "formats": []}

    def inspect_storage(self):
        return {"localStorage": ["device"]}

    def close(self):
        self.closed += 1


def _settings(tmp_path):
    return Settings(
        download_dir=str(tmp_path / "downloads"),
        task_db_path=str(tmp_path / "tasks.db"),
        risk_log_path=str(tmp_path / "risk.jsonl"),
        cookies_cache_path=str(tmp_path / "cookies.json"),
        chrome_profile_dir=str(tmp_path / "profile"),
        device_info_path=str(tmp_path / "device.json"),
    )


def test_runtime_download_and_task_snapshots(tmp_path):
    runtime = WebRuntime(_settings(tmp_path), facade=FakeFacade(),
                         persist_settings=False)

    started = runtime.start_download(
        mode="track", target="11", quality="standard",
    )
    finished = runtime.wait()
    tasks = runtime.tasks_snapshot()

    assert started["kind"] == "download_track"
    assert finished["status"] == "succeeded"
    assert finished["progress_done"] == 100
    assert finished["result"]["path"] == "/tmp/test.mp3"
    assert tasks["counts"] == {
        "all": 2, "pending": 0, "downloading": 1, "done": 1, "failed": 0,
    }
    assert tasks["tasks"][0]["progress"] == 25


def test_runtime_enforces_single_operation_and_stops_gracefully(tmp_path):
    facade = FakeFacade(blocking=True)
    runtime = WebRuntime(_settings(tmp_path), facade=facade,
                         persist_settings=False)

    runtime.start_download(mode="track", target="11")
    assert facade.started.wait(1)
    with pytest.raises(OperationBusyError, match="正在运行"):
        runtime.start_resume()

    stopping = runtime.request_stop()
    finished = runtime.wait()

    assert stopping["stop_requested"] is True
    assert finished["status"] == "stopped"
    assert "用户已停止" in finished["message"]


def test_runtime_serializes_album_results(tmp_path):
    runtime = WebRuntime(_settings(tmp_path), facade=FakeFacade(),
                         persist_settings=False)

    runtime.start_download(mode="album", target="22", range_="1-2")
    finished = runtime.wait()

    assert finished["result"]["album"]["album_title"] == "测试专辑"
    assert finished["result"]["album"]["downloaded"] == ["a.mp3"]


def test_runtime_rebuilds_facade_after_setting_change(tmp_path):
    old = FakeFacade()
    built = []

    def factory(settings):
        built.append(settings)
        return FakeFacade()

    runtime = WebRuntime(
        _settings(tmp_path), facade=old, facade_factory=factory,
        persist_settings=False,
    )

    settings = runtime.update_settings({
        "download_dir": str(tmp_path / "new"),
        "max_concurrency": 2,
    })

    assert settings["download_dir"] == str(tmp_path / "new")
    assert settings["max_concurrency"] == 2
    assert built[0].max_concurrency == 2
    assert old.closed == 1


def test_runtime_open_downloads_uses_configured_directory(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(
        "xdl.frontends.web_runtime._open_directory", opened.append,
    )
    runtime = WebRuntime(_settings(tmp_path), facade=FakeFacade(),
                         persist_settings=False)

    result = runtime.open_downloads()

    assert result["path"] == os.path.abspath(tmp_path / "downloads")
    assert opened == [result["path"]]
