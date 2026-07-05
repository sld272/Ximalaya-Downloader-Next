# -*- coding: utf-8 -*-
"""TaskStore SQLite 契约测试。"""
import sqlite3

from xdl.adapters import SqliteTaskStore
from xdl.domain import DownloadTask, TaskState


def _task(track_id="1", quality="standard", album_id="a", index=1):
    return DownloadTask(
        track_id=track_id,
        album_id=album_id,
        title=f"第{index}集",
        quality=quality,
        album_index=index,
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
    finally:
        conn.close()
    assert version == "1"
    assert {"download_task", "album_sync", "meta"} <= tables


def test_upsert_pending_dedupes_and_keeps_done(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    try:
        first = store.upsert_pending([_task()])[0]
        assert first.id is not None
        second = store.upsert_pending([_task()])[0]
        assert second.id == first.id

        store.mark_downloading(first.id)
        store.mark_done(first.id, "/tmp/final.mp3")
        assert store.upsert_pending([_task()]) == []
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
        store.mark_failed(two.id, "network", "timeout")
        store.mark_downloading(three.id)
        store.mark_failed(three.id, "auth", "denied")

        assert store.requeue_stale() == 1
        assert store.requeue_retryable_failed() == 1
        pending = store.pending_tasks("a")
        assert [t.track_id for t in pending] == ["1", "2"]
        assert all(t.state is TaskState.PENDING for t in pending)
        assert store.pending_albums() == [("a", "a", 2)]
    finally:
        store.close()


def test_progress_and_album_cursor(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    try:
        task = store.upsert_pending([_task()])[0]
        store.record_progress(task.id, 1024, 4096)
        updated = store.pending_tasks("a")[0]
        assert updated.bytes_done == 1024
        assert updated.total_bytes == 4096

        store.save_album_meta("a", "专辑", 20)
        store.save_album_cursor("a", "cursor-1")
        assert store.album_cursor("a") == "cursor-1"
        assert store.pending_albums() == [("a", "专辑", 1)]
    finally:
        store.close()
