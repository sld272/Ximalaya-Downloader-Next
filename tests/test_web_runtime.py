# -*- coding: utf-8 -*-
import os
import threading

import pytest

from xdl.application.usecases import AlbumResult
from xdl.domain import DownloadTask, TaskState
from xdl.errors import CancelledByUser
from xdl.frontends.web_runtime import (OperationBusyError,
                                       WebRuntime)
from xdl.ports import (TaskDeleteResult, TaskQueryResult,
                       TaskSelectionSummary)
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

    def _scoped(self, state, search, album_id):
        rows = self.tasks
        if state is not None:
            rows = [task for task in rows if task.state is state]
        if album_id:
            rows = [task for task in rows if task.album_id == album_id]
        query = search.casefold().strip()
        if query:
            rows = [task for task in rows if query in (
                f"{task.title} {task.track_id} {task.album_id}".casefold()
            )]
        return rows

    def query_task_ids(self, *, state=None, search="", album_id="", cap=5000):
        return [task.id for task in self._scoped(state, search, album_id)][:cap]

    def summarize_tasks(self, ids):
        found = [task for task in self.tasks if task.id in set(ids)]
        states = {}
        running = 0
        for task in found:
            if task.state is TaskState.DOWNLOADING:
                running += 1
                continue
            states[task.state.value] = states.get(task.state.value, 0) + 1
        return TaskSelectionSummary(
            states=states, running=running,
            missing=len(set(ids)) - len(found),
        )

    def delete_tasks(self, ids):
        wanted = set(ids)
        killable = [task for task in self.tasks
                    if task.id in wanted and task.state is not TaskState.DOWNLOADING]
        running = len([task for task in self.tasks
                       if task.id in wanted and task.state is TaskState.DOWNLOADING])
        found = len([task for task in self.tasks if task.id in wanted])
        self.tasks = [task for task in self.tasks if task not in killable]
        return TaskDeleteResult(
            deleted=len(killable), skipped_running=running,
            missing=len(wanted) - found,
        )

    def requeue_tasks(self, ids):
        wanted = set(ids)
        changed = 0
        for task in self.tasks:
            if task.id in wanted and task.state is TaskState.FAILED:
                task.state = TaskState.PENDING
                changed += 1
        return changed

    def query_tasks(self, *, state=None, search="", album_id="",
                    limit=100, offset=0):
        rows = self._scoped(state, search, album_id)
        counts = {task_state: 0 for task_state in TaskState}
        for task in self.tasks:
            counts[task.state] += 1
        total = len(rows)
        if total and offset >= total:
            offset = ((total - 1) // limit) * limit
        return TaskQueryResult(
            tasks=rows[offset:offset + limit], total=total,
            counts=counts, offset=offset, limit=limit,
        )

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

    def login_password(self, account, password, mode, fds_otp):
        return {"authenticated": True, "account": account, "password": password,
                "mode": mode, "fds_otp": fds_otp}

    def switch_account(self, uid):
        return {"authenticated": True, "uid": uid}

    def delete_account(self, uid):
        return {"authenticated": False, "uid": "", "deleted": uid}

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
    assert tasks["page"] == {
        "offset": 0, "limit": 100, "total": 2,
        "has_previous": False, "has_next": False,
    }


def test_runtime_forwards_apk_password_login(tmp_path):
    runtime = WebRuntime(_settings(tmp_path), facade=FakeFacade(),
                         persist_settings=False)

    result = runtime.apk_login_password(
        "13800138000", "secret", "mobile", {"lot_number": "lot"},
    )

    assert result == {
        "authenticated": True, "account": "13800138000", "password": "secret",
        "mode": "mobile", "fds_otp": {"lot_number": "lot"},
    }


def test_runtime_switches_apk_account_only_while_idle(tmp_path):
    facade = FakeFacade()
    runtime = WebRuntime(_settings(tmp_path), facade=facade,
                         persist_settings=False)

    assert runtime.apk_switch_account("200")["uid"] == "200"
    assert runtime.apk_delete_account("200")["deleted"] == "200"

    runtime._operation = {"status": "running", "label": "恢复全部"}
    with pytest.raises(OperationBusyError, match="正在运行"):
        runtime.apk_switch_account("100")
    with pytest.raises(OperationBusyError, match="正在运行"):
        runtime.apk_login_password(
            "13800138000", "secret", "mobile", {"lot_number": "lot"},
        )


def test_runtime_filters_and_pages_task_snapshots(tmp_path):
    runtime = WebRuntime(_settings(tmp_path), facade=FakeFacade(),
                         persist_settings=False)

    tasks = runtime.tasks_snapshot(
        state="done", search="第二", limit=1, offset=0,
    )

    assert [task["track_id"] for task in tasks["tasks"]] == ["12"]
    assert tasks["page"]["total"] == 1
    assert tasks["counts"]["all"] == 2
    assert tasks["counts"]["done"] == 1


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
    lightweight = runtime.operation_snapshot(include_result=False)
    assert lightweight["has_result"] is True
    assert "result" not in lightweight


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


def test_runtime_delete_tasks_reports_detail_and_refreshes(tmp_path):
    runtime = WebRuntime(_settings(tmp_path), facade=FakeFacade(),
                         persist_settings=False)

    payload = runtime.delete_tasks([1, 2, 2, 4242])

    # 1 是 downloading，删不掉；2 可删；4242 不存在
    assert payload["result"]["deleted"] == 1
    assert payload["result"]["skipped_running"] == 1
    assert payload["result"]["missing"] == 1
    # 删完顺带回一份新快照，前端不用再发一次请求
    assert [task["id"] for task in payload["tasks"]] == [1]


def test_runtime_delete_tasks_rejects_oversized_selection(tmp_path):
    runtime = WebRuntime(_settings(tmp_path), facade=FakeFacade(),
                         persist_settings=False)

    with pytest.raises(ValueError, match="一次最多操作"):
        runtime.delete_tasks(list(range(6000)))


def test_runtime_delete_works_while_an_operation_runs(tmp_path):
    """删除不走操作锁：挂着大专辑跑几小时时也得能清理历史。"""
    facade = FakeFacade(blocking=True)
    runtime = WebRuntime(_settings(tmp_path), facade=facade,
                         persist_settings=False)
    runtime.start_download(mode="track", target="123")
    assert facade.started.wait(2)

    payload = runtime.delete_tasks([2])

    assert payload["result"]["deleted"] == 1
    runtime.request_stop()
    runtime.wait()


def test_runtime_task_ids_snapshot_is_scoped(tmp_path):
    runtime = WebRuntime(_settings(tmp_path), facade=FakeFacade(),
                         persist_settings=False)

    assert runtime.task_ids()["count"] == 2
    assert runtime.task_ids(state="done")["ids"] == [2]
    assert runtime.task_ids(album_id="nope")["ids"] == []
    assert runtime.task_ids()["truncated"] is False


def test_runtime_requeue_tasks_counts_changes(tmp_path):
    facade = FakeFacade()
    facade.tasks[1].state = TaskState.FAILED
    runtime = WebRuntime(_settings(tmp_path), facade=facade,
                         persist_settings=False)

    payload = runtime.requeue_tasks([1, 2])

    assert payload["requeued"] == 1     # 只有 failed 那条被放回队列
