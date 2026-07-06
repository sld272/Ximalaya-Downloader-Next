# -*- coding: utf-8 -*-
"""TaskStore SQLite 契约测试。"""
import sqlite3

import pytest

from xdl.adapters import SqliteTaskStore
from xdl.domain import DownloadTask, TaskState
from xdl.errors import StorageError


def _task(track_id="1", quality="standard", album_id="a", index=1):
    return DownloadTask(
        track_id=track_id,
        album_id=album_id,
        title=f"第{index}集",
        quality=quality,
        album_index=index,
        index_width=2,
    )


def test_store_migrates_empty_db(tmp_path):
    path = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(path))
    store.close()

    conn = sqlite3.connect(path)
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        columns = {r[1] for r in conn.execute("PRAGMA table_info(download_task)")}
    finally:
        conn.close()
    assert version == "2"
    assert {"download_task", "album_sync", "meta"} <= tables
    assert {"retryable", "index_width"} <= columns


def test_upsert_pending_dedupes_and_keeps_done(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    try:
        first = store.upsert_pending([_task()])[0]
        assert first.id is not None
        second = store.upsert_pending([_task()])[0]
        assert second.id == first.id

        store.mark_downloading(first.id)
        store.mark_done(first.id, "/tmp/final.mp3")
        done = store.upsert_pending([_task()])
        assert len(done) == 1
        assert done[0].id == first.id
        assert done[0].state is TaskState.DONE
        assert store.pending_tasks("a") == []
    finally:
        store.close()


def test_requeue_stale_and_retryable_failed(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    try:
        one, two, three = store.upsert_pending([
            _task("1", index=1),
            _task("2", index=2),
            _task("3", index=3),
        ])
        store.mark_downloading(one.id)
        store.mark_downloading(two.id)
        store.mark_failed(two.id, "network", "timeout", True)
        store.mark_downloading(three.id)
        store.mark_failed(three.id, "api", "not found", False)

        assert store.requeue_stale() == 1
        assert store.requeue_retryable_failed() == 1
        pending = store.pending_tasks("a")
        assert [t.track_id for t in pending] == ["1", "2"]
        assert all(t.state is TaskState.PENDING for t in pending)
        assert store.pending_albums() == [("a", "a", 2)]
    finally:
        store.close()


def test_done_task_ignores_late_failure_update(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    try:
        task = store.upsert_pending([_task()])[0]
        store.mark_downloading(task.id)
        store.mark_done(task.id, "/tmp/final.mp3")
        store.mark_failed(task.id, "api", "late failure", False)

        row = store.upsert_pending([_task()])[0]
        assert row.state is TaskState.DONE
        assert row.last_error_code == ""
    finally:
        store.close()


def test_progress_and_album_cursor(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    try:
        task = store.upsert_pending([_task()])[0]
        store.mark_downloading(task.id)
        store.record_progress(task.id, 1024, 4096)
        store.requeue_stale()
        updated = store.pending_tasks("a")[0]
        assert updated.bytes_done == 1024
        assert updated.total_bytes == 4096

        store.save_album_meta("a", "专辑", 20)
        store.save_album_cursor("a", "cursor-1")
        assert store.album_cursor("a") == "cursor-1"
        assert store.album_total("a") == 20
        assert store.pending_albums() == [("a", "专辑", 1)]
    finally:
        store.close()


def test_store_wraps_connect_errors(monkeypatch, tmp_path):
    def fail_connect(*args, **kwargs):
        raise sqlite3.DatabaseError("broken")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)

    with pytest.raises(StorageError, match="任务库不可用"):
        SqliteTaskStore(str(tmp_path / "tasks.db"))


def test_store_reports_existing_instance_lock(monkeypatch, tmp_path):
    monkeypatch.setattr("xdl.adapters.store_sqlite._lock_file", lambda f: False)

    with pytest.raises(StorageError, match="已有 xdl 实例"):
        SqliteTaskStore(str(tmp_path / "tasks.db"))


def test_store_wraps_method_sqlite_errors():
    store = SqliteTaskStore(":memory:")
    store.close()

    with pytest.raises(StorageError, match="任务库操作失败"):
        store.pending_albums()
