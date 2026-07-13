# -*- coding: utf-8 -*-
"""用例层单测：错误分级退避重试 + 失败收尾轮 + 并发（用替身，零真实 I/O）。"""
import asyncio
import os
import sqlite3

import pytest

from xdl.adapters import SqliteTaskStore
from xdl.domain import Album, AlbumTrack, DownloadTask, Track, PlayUrl, Quality
from xdl.application.usecases import (DownloadTrackUseCase, DownloadAlbumUseCase,
                                      ResumeUseCase, RetryPolicy)
from xdl.errors import NetworkError, AuthError, ApiError, CancelledByUser

# 退避/冷却全置 0，测试不真正 sleep
FAST = RetryPolicy(max_attempts=3, backoff_base=0, cooldown=0, global_rounds=2)


def run(coro):
    return asyncio.run(coro)


class FakeSink:
    def __init__(self):
        self.writes = []

    def write(self, url, path, reporter, cancel=None, progress_sink=None,
              expected_total=0):   # 同步，用例里经 to_thread 调用
        self.writes.append((url, path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if progress_sink:
            progress_sink(1, 1)
        open(path, "w").close()


class CancelSink(FakeSink):
    def write(self, url, path, reporter, cancel=None, progress_sink=None,
              expected_total=0):
        self.writes.append((url, path))
        raise CancelledByUser("stop")


class _FailSink(FakeSink):
    def write(self, url, path, reporter, cancel=None, progress_sink=None,
              expected_total=0):
        self.writes.append((url, path))
        raise ApiError("下载失败")   # 默认 retryable=False


class FakeSource:
    """behavior: track_id -> 结果序列；元素为异常实例（抛出）或 'ok'（返回 Track）。
    最后一个元素重复使用；calls 记录每个 track 调用次数。"""
    def __init__(self, album=None, behavior=None):
        self.album = album
        self.behavior = behavior or {}
        self.calls: dict[str, int] = {}

    async def open(self): pass
    async def close(self): pass
    async def get_album(self, album_id): return self.album

    async def get_track(self, track_id):
        self.calls[track_id] = self.calls.get(track_id, 0) + 1
        seq = self.behavior.get(track_id, ["ok"])
        out = seq[min(self.calls[track_id] - 1, len(seq) - 1)]
        if isinstance(out, Exception):
            raise out
        return Track(track_id=track_id, title=f"t{track_id}",
                     play_urls=[PlayUrl("MP3_64", f"http://x/{track_id}.mp3")])


def test_album_usecase_defaults_to_single_concurrency(tmp_path):
    uc = DownloadAlbumUseCase(FakeSource(), FakeSink(), str(tmp_path))
    assert uc._concurrency == 1


def test_track_retry_then_success(tmp_path):
    src = FakeSource(behavior={"1": [NetworkError("a"), NetworkError("b"), "ok"]})
    uc = DownloadTrackUseCase(src, FakeSink(), str(tmp_path), retry=FAST)
    path = run(uc.execute("1", Quality.STANDARD))
    assert path.endswith(".mp3")
    assert src.calls["1"] == 3


def test_track_non_retryable_no_retry(tmp_path):
    src = FakeSource(behavior={"1": [AuthError("无权")]})
    uc = DownloadTrackUseCase(src, FakeSink(), str(tmp_path), retry=FAST)
    with pytest.raises(AuthError):
        run(uc.execute("1", Quality.STANDARD))
    assert src.calls["1"] == 1


def test_track_persists_task_to_store(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        uc = DownloadTrackUseCase(FakeSource(), FakeSink(), str(tmp_path),
                                  retry=FAST, store=store)
        run(uc.execute("7", Quality.STANDARD))
        rows = _db_rows(db)
        assert len(rows) == 1
        assert rows[0]["track_id"] == "7"
        assert rows[0]["album_id"] == ""      # 单曲无专辑
        assert rows[0]["state"] == "done"
        # 单曲同样出现在面板数据源里
        assert [t.track_id for t in store.all_tasks()] == ["7"]
    finally:
        store.close()


def test_track_failure_persists_as_failed(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        # 解析成功但下载抛非重试错误 → 落 failed 终态、面板可见
        src = FakeSource()
        uc = DownloadTrackUseCase(src, _FailSink(), str(tmp_path),
                                  retry=FAST, store=store)
        with pytest.raises(ApiError):
            run(uc.execute("7", Quality.STANDARD))
        rows = _db_rows(db)
        assert len(rows) == 1
        assert rows[0]["state"] == "failed"
        assert rows[0]["retryable"] == 0
    finally:
        store.close()


def test_track_retryable_exhausted(tmp_path):
    src = FakeSource(behavior={"1": [NetworkError("x")]})
    uc = DownloadTrackUseCase(src, FakeSink(), str(tmp_path),
                              retry=RetryPolicy(max_attempts=2, backoff_base=0))
    with pytest.raises(NetworkError):
        run(uc.execute("1", Quality.STANDARD))
    assert src.calls["1"] == 2


def _one_track_album():
    return Album("123", "专辑", total=1,
                 tracks=[AlbumTrack(track_id="1", title="第1集", index=1)])


def _db_rows(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM download_task ORDER BY album_index"
        ).fetchall()]
    finally:
        conn.close()


def test_album_recovery_round_salvages(tmp_path):
    src = FakeSource(_one_track_album(), behavior={
        "1": [NetworkError("1"), NetworkError("2"), NetworkError("3"), "ok"]})
    uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path), concurrency=2,
                              retry=RetryPolicy(max_attempts=2, backoff_base=0,
                                                cooldown=0, global_rounds=2))
    res = run(uc.execute("123", Quality.STANDARD))
    assert len(res.downloaded) == 1 and not res.failed
    assert src.calls["1"] == 4


def test_album_non_retryable_stays_failed(tmp_path):
    src = FakeSource(_one_track_album(), behavior={"1": [AuthError("无权")]})
    uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path), retry=FAST)
    res = run(uc.execute("123", Quality.STANDARD))
    assert len(res.failed) == 1
    assert src.calls["1"] == 1


def test_album_rate_limit_is_retryable(tmp_path):
    src = FakeSource(_one_track_album(), behavior={
        "1": [ApiError("风控", ret=1001, retryable=True), "ok"]})
    uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path),
                              retry=RetryPolicy(max_attempts=1, backoff_base=0,
                                                cooldown=0, global_rounds=2))
    res = run(uc.execute("123", Quality.STANDARD))
    assert len(res.downloaded) == 1 and not res.failed
    assert src.calls["1"] == 2


def test_album_concurrent_all_downloaded(tmp_path):
    # 10 集全部成功，并发 4，应全下载、无失败，且每集各解析一次
    tracks = [AlbumTrack(track_id=str(i), title=f"第{i}集", index=i) for i in range(1, 11)]
    album = Album("123", "专辑", total=10, tracks=tracks)
    src = FakeSource(album)
    uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path), concurrency=4, retry=FAST)
    res = run(uc.execute("123", Quality.STANDARD))
    assert len(res.downloaded) == 10 and not res.failed and not res.skipped
    assert all(src.calls[str(i)] == 1 for i in range(1, 11))


def test_album_store_marks_success_done(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        src = FakeSource(_one_track_album())
        sink = FakeSink()
        uc = DownloadAlbumUseCase(src, sink, str(tmp_path / "downloads"),
                                  retry=FAST, store=store)
        res = run(uc.execute("123", Quality.STANDARD))
        assert len(res.downloaded) == 1 and not res.failed
        rows = _db_rows(db)
        assert [(r["state"], r["target_path"]) for r in rows] == [
            ("done", res.downloaded[0])
        ]
        assert rows[0]["index_width"] == 1
        assert store.pending_tasks("123") == []
    finally:
        store.close()


def test_album_store_records_failed_error(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        src = FakeSource(_one_track_album(), behavior={"1": [NetworkError("boom")]})
        uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path / "downloads"),
                                  retry=RetryPolicy(max_attempts=1, backoff_base=0,
                                                    cooldown=0, global_rounds=0),
                                  store=store)
        res = run(uc.execute("123", Quality.STANDARD))
        assert len(res.failed) == 1
        rows = _db_rows(db)
        assert rows[0]["state"] == "failed"
        assert rows[0]["last_error_code"] == "network"
        assert rows[0]["retryable"] == 1
        assert rows[0]["attempts"] == 1
    finally:
        store.close()


def test_album_stop_before_dispatch_keeps_pending(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        src = FakeSource(_one_track_album())
        sink = FakeSink()

        async def scenario():
            stop = asyncio.Event()
            stop.set()
            uc = DownloadAlbumUseCase(src, sink, str(tmp_path / "downloads"),
                                      retry=FAST, store=store, stop_event=stop)
            return await uc.execute("123", Quality.STANDARD)

        res = run(scenario())
        rows = _db_rows(db)
        assert not res.downloaded and not res.failed
        assert sink.writes == []
        assert rows[0]["state"] == "pending"
        assert rows[0]["attempts"] == 0
    finally:
        store.close()


def test_album_cancelled_write_requeues_task(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        src = FakeSource(_one_track_album())
        sink = CancelSink()
        uc = DownloadAlbumUseCase(src, sink, str(tmp_path / "downloads"),
                                  retry=FAST, store=store)
        res = run(uc.execute("123", Quality.STANDARD))
        rows = _db_rows(db)
        assert not res.downloaded and not res.failed
        assert len(sink.writes) == 1
        assert rows[0]["state"] == "pending"
        assert rows[0]["attempts"] == 1
    finally:
        store.close()


def test_album_second_execute_skips_done_file_without_redownload(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        src = FakeSource(_one_track_album())
        sink = FakeSink()
        uc = DownloadAlbumUseCase(src, sink, str(tmp_path / "downloads"),
                                  retry=FAST, store=store)
        first = run(uc.execute("123", Quality.STANDARD))
        second = run(uc.execute("123", Quality.STANDARD))
        assert len(first.downloaded) == 1
        assert len(second.skipped) == 1 and not second.downloaded
        assert len(sink.writes) == 1
        assert src.calls["1"] == 1
    finally:
        store.close()


def test_album_redownload_missing_done_file_is_requeueable(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        src = FakeSource(_one_track_album())
        sink = FakeSink()
        download_dir = tmp_path / "downloads"
        uc = DownloadAlbumUseCase(src, sink, str(download_dir),
                                  retry=FAST, store=store)
        first = run(uc.execute("123", Quality.STANDARD))
        os.remove(first.downloaded[0])

        cancelled = DownloadAlbumUseCase(
            src, CancelSink(), str(download_dir), retry=FAST, store=store,
        )
        second = run(cancelled.execute("123", Quality.STANDARD))
        rows = _db_rows(db)
        assert not second.downloaded and not second.failed
        assert rows[0]["state"] == "pending"
        assert rows[0]["attempts"] == 2
    finally:
        store.close()


def test_resume_usecase_runs_pending_and_stale_tasks(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        store.save_album_meta("123", "专辑", 2)
        tasks = store.upsert_pending([
            DownloadTask("1", "123", "第1集", Quality.STANDARD.value, 1),
            DownloadTask("2", "123", "第2集", Quality.STANDARD.value, 2),
        ])
        store.mark_downloading(tasks[0].id)

        src = FakeSource()
        sink = FakeSink()
        uc = ResumeUseCase(src, sink, str(tmp_path / "downloads"), store,
                           concurrency=2, retry=FAST)
        results = run(uc.execute())
        assert len(results) == 1
        assert len(results[0].downloaded) == 2
        assert not results[0].failed
        assert store.pending_tasks("123") == []
        assert [r["state"] for r in _db_rows(db)] == ["done", "done"]
    finally:
        store.close()


def test_resume_marks_unknown_quality_as_terminal_failure(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        store.save_album_meta("123", "专辑", 1)
        store.upsert_pending([
            DownloadTask("1", "123", "第1集", "hq", 1),
        ])

        uc = ResumeUseCase(FakeSource(), FakeSink(), str(tmp_path / "downloads"),
                           store, retry=FAST)
        results = run(uc.execute())
        rows = _db_rows(db)
        assert len(results) == 1
        assert len(results[0].failed) == 1
        assert rows[0]["state"] == "failed"
        assert rows[0]["last_error_msg"] == "未知音质: hq"
        assert rows[0]["retryable"] == 0
        assert store.pending_albums() == []
    finally:
        store.close()


def test_resume_uses_persisted_index_width_for_existing_files(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        store.save_album_meta("123", "专辑", 0)
        store.upsert_pending([
            DownloadTask("3", "123", "第3集", Quality.STANDARD.value, 3,
                         index_width=2),
        ])
        album_dir = tmp_path / "downloads" / "专辑"
        album_dir.mkdir(parents=True)
        existing = album_dir / "03 第3集.mp3"
        existing.write_text("", encoding="utf-8")

        sink = FakeSink()
        uc = ResumeUseCase(FakeSource(), sink, str(tmp_path / "downloads"),
                           store, retry=FAST)
        results = run(uc.execute())
        rows = _db_rows(db)
        assert results[0].skipped == [str(existing)]
        assert sink.writes == []
        assert rows[0]["state"] == "done"
    finally:
        store.close()
