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
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(download_task)")}
    finally:
        conn.close()
    assert version == "3"
    assert {"download_task", "album_sync", "meta"} <= tables
    assert {"retryable", "index_width"} <= columns
    assert "idx_task_state_id" in indexes


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


def test_requeue_failed_category_after_auth_recovers(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    try:
        auth, api = store.upsert_pending([
            _task("1", index=1), _task("2", index=2),
        ])
        store.mark_failed(auth.id, "auth", "请先登录", False)
        store.mark_failed(api.id, "api", "已下架", False)

        assert store.requeue_failed_category("auth") == 1
        assert [task.track_id for task in store.pending_tasks("a")] == ["1"]
        failed = store.query_tasks(state=TaskState.FAILED)
        assert [task.track_id for task in failed.tasks] == ["2"]
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


def test_query_tasks_pages_filters_searches_and_counts(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    try:
        special = _task("5", album_id="special", index=5)
        special.title = "100% 特别节目"
        tasks = store.upsert_pending([
            _task("1", index=1),
            _task("2", index=2),
            _task("3", index=3),
            _task("4", index=4),
            special,
        ])
        store.mark_downloading(tasks[0].id)
        store.mark_done(tasks[0].id, "/tmp/one.mp3")
        store.mark_downloading(tasks[1].id)
        store.mark_failed(tasks[1].id, "network", "timeout", False)
        store.mark_downloading(tasks[2].id)

        first = store.query_tasks(limit=2)
        assert [task.track_id for task in first.tasks] == ["3", "2"]
        assert first.total == 5
        assert first.offset == 0
        assert first.limit == 2
        assert first.counts == {
            TaskState.PENDING: 2,
            TaskState.DOWNLOADING: 1,
            TaskState.DONE: 1,
            TaskState.FAILED: 1,
        }

        last_pending = store.query_tasks(
            state=TaskState.PENDING, limit=1, offset=999,
        )
        assert last_pending.total == 2
        assert last_pending.offset == 1
        assert [task.track_id for task in last_pending.tasks] == ["4"]

        searched = store.query_tasks(search="100%")
        assert searched.total == 1
        assert [task.track_id for task in searched.tasks] == ["5"]

        # 状态刚更新的旧任务应回到第一页，不能被创建时的旧 id 固定在深页。
        store.mark_done(tasks[0].id, "/tmp/one-again.mp3")
        refreshed = store.query_tasks(limit=2)
        assert refreshed.tasks[0].track_id == "1"
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


def _seed(store, tasks):
    """写入任务并返回带 id 的行。"""
    return store.upsert_pending(tasks)


def test_query_tasks_album_filter_is_exact(tmp_path):
    """album_id 精确匹配：它圈定的是删除范围，模糊会跨专辑误伤。"""
    store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    try:
        _seed(store, [
            _task(track_id="1", album_id="1234", index=1),
            _task(track_id="2", album_id="1234", index=2),
            _task(track_id="3", album_id="12345", index=1),
        ])

        assert store.query_tasks(album_id="1234").total == 2
        # 搜索仍是模糊的——它只用来找东西
        assert store.query_tasks(search="1234").total == 3
        assert store.query_task_ids(album_id="12345") != []
        assert len(store.query_task_ids(album_id="1234")) == 2
    finally:
        store.close()


def test_delete_tasks_skips_running_and_clears_part_files(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    part = tmp_path / "第2集.part"
    part.write_text("half", encoding="utf-8")
    (tmp_path / "第2集.part.meta").write_text("{}", encoding="utf-8")
    try:
        rows = _seed(store, [_task(track_id="1", index=1),
                             _task(track_id="2", index=2)])
        store.record_progress(rows[1].id, 4, 8)  # pending 状态下是 no-op，仅为对齐真实调用
        conn = sqlite3.connect(tmp_path / "tasks.db")
        conn.execute("UPDATE download_task SET part_path=? WHERE id=?",
                     (str(part), rows[1].id))
        conn.commit()
        conn.close()
        store.mark_downloading(rows[0].id)

        result = store.delete_tasks([rows[0].id, rows[1].id, 4242])

        assert result.deleted == 1
        assert result.skipped_running == 1     # 运行中的删不掉
        assert result.missing == 1             # 不存在的 id 静默跳过
        assert result.files_removed == 2       # .part 和 .part.meta
        assert not part.exists()
        assert [t.id for t in store.all_tasks()] == [rows[0].id]
    finally:
        store.close()


def test_delete_tasks_prunes_orphan_album_rows(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    try:
        rows = _seed(store, [_task(track_id="1", album_id="keep", index=1),
                             _task(track_id="2", album_id="gone", index=1)])
        store.save_album_meta("keep", "留下", 10)
        store.save_album_meta("gone", "删光", 10)

        store.delete_tasks([rows[1].id])

        assert store.album_total("keep") == 10
        assert store.album_total("gone") == 0   # 孤儿元数据已清
    finally:
        store.close()


def test_summarize_tasks_counts_by_state_and_part_bytes(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    try:
        rows = _seed(store, [_task(track_id=str(i), index=i) for i in (1, 2, 3)])
        store.mark_done(rows[0].id, "/out/1.m4a")
        store.mark_downloading(rows[1].id)
        store.record_progress(rows[1].id, 512, 1024)
        conn = sqlite3.connect(tmp_path / "tasks.db")
        conn.execute("UPDATE download_task SET part_path=? WHERE id=?",
                     ("/out/3.part", rows[2].id))
        conn.execute("UPDATE download_task SET bytes_done=64 WHERE id=?",
                     (rows[2].id,))
        conn.commit()
        conn.close()

        summary = store.summarize_tasks([r.id for r in rows] + [999])

        assert summary.states == {"done": 1, "pending": 1}
        assert summary.running == 1        # downloading 单独计，不进 states
        assert summary.with_part == 1
        assert summary.part_bytes == 64
        assert summary.missing == 1
    finally:
        store.close()


def test_requeue_tasks_only_revives_failed(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    try:
        rows = _seed(store, [_task(track_id="1", index=1),
                             _task(track_id="2", index=2)])
        store.mark_failed(rows[0].id, "api", "已下架", False)

        assert store.requeue_tasks([rows[0].id, rows[1].id]) == 1

        states = {t.id: t.state for t in store.all_tasks()}
        assert states[rows[0].id] is TaskState.PENDING
        assert store.query_tasks(state=TaskState.FAILED).total == 0
    finally:
        store.close()


def test_delete_tasks_handles_empty_and_duplicate_ids(tmp_path):
    store = SqliteTaskStore(str(tmp_path / "tasks.db"))
    try:
        rows = _seed(store, [_task(track_id="1", index=1)])

        assert store.delete_tasks([]).deleted == 0
        result = store.delete_tasks([rows[0].id, rows[0].id])
        assert result.deleted == 1 and result.missing == 0
    finally:
        store.close()
