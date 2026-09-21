# -*- coding: utf-8 -*-
"""用例层单测：错误分级退避重试 + 失败收尾轮 + 并发（用替身，零真实 I/O）。"""
import asyncio
import os
import sqlite3

import pytest

from xdl.adapters import SqliteTaskStore
from xdl.domain import Album, AlbumTrack, DownloadTask, Track, PlayUrl, Quality
from xdl.application.usecases import (DownloadTrackUseCase, DownloadAlbumUseCase,
                                      ResumeUseCase, RetryPolicy,
                                      RiskRecoveryPolicy)
from xdl.errors import (NetworkError, AuthError, ApiError, CancelledByUser,
                        ConsecutiveFailureError, DownloadLimitError,
                        RiskControlError)

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
    assert (uc._resolve_concurrency, uc._download_concurrency) == (1, 1)


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


def test_apk_three_consecutive_failures_abort_batch_and_keep_later_pending(tmp_path):
    tracks = [AlbumTrack(str(i), f"第{i}集", i) for i in range(1, 6)]
    album = Album("123", "专辑", total=5, tracks=tracks)
    src = FakeSource(album, behavior={
        "1": [DownloadLimitError("已超过体验会员下载上限")],
        "2": [AuthError("无权")],
        "3": [ApiError("接口拒绝")],
    })
    src.max_consecutive_failures = 3
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        uc = DownloadAlbumUseCase(
            src, FakeSink(), str(tmp_path / "downloads"), concurrency=1,
            retry=RetryPolicy(max_attempts=1, backoff_base=0, cooldown=0,
                              global_rounds=0),
            store=store,
        )

        with pytest.raises(ConsecutiveFailureError, match="连续 3 集解析失败"):
            run(uc.execute("123", Quality.STANDARD))

        rows = _db_rows(db)
        assert [(row["track_id"], row["state"]) for row in rows] == [
            ("1", "failed"), ("2", "failed"), ("3", "failed"),
            ("4", "pending"), ("5", "pending"),
        ]
        assert rows[0]["last_error_code"] == "download_limit"
        assert rows[0]["retryable"] == 0
        assert src.calls == {"1": 1, "2": 1, "3": 1}
    finally:
        store.close()


def test_apk_consecutive_failure_breaker_forces_serial_order(tmp_path):
    src = FakeSource(_one_track_album())
    src.max_consecutive_failures = 3

    uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path), concurrency=8)

    # 熔断计数依赖集序，解析必须串行；但下载不受限，并发保持用户值。
    assert (uc._resolve_concurrency, uc._download_concurrency) == (1, 8)


def test_forced_serial_download_is_announced_when_user_asked_for_more(tmp_path):
    """用户显式传了并发时必须通过 reporter 告知解析仍是串行——不能静默。"""
    src = FakeSource(_one_track_album())
    src.max_consecutive_failures = 3
    notes = []

    class _Reporter:
        def note(self, msg):
            notes.append(msg)

    uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path), concurrency=8)
    run(uc.execute("123", Quality.STANDARD, reporter=_Reporter()))

    assert any("解析需串行" in note for note in notes)


def test_serial_download_not_announced_when_user_did_not_ask_for_more(tmp_path):
    """用户没要并发（默认 1）时不该多出一条无谓的提示。"""
    src = FakeSource(_one_track_album())
    src.max_consecutive_failures = 3
    notes = []

    class _Reporter:
        def note(self, msg):
            notes.append(msg)

    uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path), concurrency=1)
    run(uc.execute("123", Quality.STANDARD, reporter=_Reporter()))

    assert not any("解析需串行" in note for note in notes)


def test_apk_success_resets_consecutive_failure_count(tmp_path):
    tracks = [AlbumTrack(str(i), f"第{i}集", i) for i in range(1, 6)]
    album = Album("123", "专辑", total=5, tracks=tracks)
    src = FakeSource(album, behavior={
        "1": [ApiError("失败1")], "2": [ApiError("失败2")],
        "4": [ApiError("失败3")], "5": [ApiError("失败4")],
    })
    src.max_consecutive_failures = 3
    uc = DownloadAlbumUseCase(
        src, FakeSink(), str(tmp_path), concurrency=1,
        retry=RetryPolicy(max_attempts=1, backoff_base=0, cooldown=0,
                          global_rounds=0),
    )

    result = run(uc.execute("123", Quality.STANDARD))

    assert len(result.downloaded) == 1
    assert len(result.failed) == 4
    assert src.calls == {str(i): 1 for i in range(1, 6)}


class _OrderSource(FakeSource):
    """记录 get_track 调用顺序，用于断言 APK 解析严格按集序。"""

    def __init__(self, album):
        super().__init__(album)
        self.order: list[str] = []

    async def get_track(self, track_id):
        self.order.append(track_id)
        return await super().get_track(track_id)


def test_apk_concurrent_download_keeps_resolve_order(tmp_path):
    """APK 并发下载：解析仍严格按集序（熔断计数前提），全部落盘。"""
    tracks = [AlbumTrack(str(i), f"第{i}集", i) for i in range(1, 6)]
    album = Album("123", "专辑", total=5, tracks=tracks)
    src = _OrderSource(album)
    src.max_consecutive_failures = 3

    uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path), concurrency=4,
                              retry=FAST)
    res = run(uc.execute("123", Quality.STANDARD))

    assert len(res.downloaded) == 5 and not res.failed
    assert src.order == [str(i) for i in range(1, 6)]


def test_apk_download_window_bounded_by_concurrency(tmp_path):
    """窗口信号量：任意时刻「已解析未完成下载」≤ 下载并发——窗口满后
    后续集的解析被挡住，受保护接口节奏仍然有界。"""
    import threading

    tracks = [AlbumTrack(str(i), f"第{i}集", i) for i in range(1, 6)]
    album = Album("123", "专辑", total=5, tracks=tracks)
    src = _OrderSource(album)
    src.max_consecutive_failures = 3

    gate = threading.Event()

    class _GatedSink(FakeSink):
        def write(self, url, path, reporter, cancel=None, progress_sink=None,
                  expected_total=0):
            self.writes.append((url, path))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not gate.wait(timeout=5):
                raise AssertionError("下载闸门未释放，测试超时")
            open(path, "w").close()

    sink = _GatedSink()

    async def scenario():
        uc = DownloadAlbumUseCase(src, sink, str(tmp_path), concurrency=2,
                                  retry=FAST)
        task = asyncio.create_task(uc.execute("123", Quality.STANDARD))
        await asyncio.sleep(0.5)   # 让 1、2 集解析完并阻塞在下载里
        assert src.order == ["1", "2"]      # 窗口已满，第 3 集解析未开始
        gate.set()
        return await asyncio.wait_for(task, timeout=5)

    res = run(scenario())
    assert len(res.downloaded) == 5 and not res.failed
    assert src.order == [str(i) for i in range(1, 6)]


def test_apk_download_stage_risk_control_breaks_batch(tmp_path):
    """下载阶段（含 APK sink 刷 URL 路径）撞风控也必须熔断整批：
    后续集零次解析请求，已解析集正常落盘。"""
    tracks = [AlbumTrack(str(i), f"第{i}集", i) for i in range(1, 6)]
    album = Album("123", "专辑", total=5, tracks=tracks)
    src = _OrderSource(album)
    src.max_consecutive_failures = 3

    class _RiskSink(FakeSink):
        def write(self, url, path, reporter, cancel=None, progress_sink=None,
                  expected_total=0):
            if url.endswith("/1.mp3"):
                super().write(url, path, reporter, cancel, progress_sink,
                              expected_total)
                return
            raise RiskControlError("风控拦截", ret=3005)

    uc = DownloadAlbumUseCase(src, _RiskSink(), str(tmp_path), concurrency=1,
                              retry=FAST)
    res = run(uc.execute("123", Quality.STANDARD))

    assert len(res.downloaded) == 1
    assert res.risk_control is not None
    assert res.deferred == 4
    assert src.order == ["1", "2"]    # 风控后 3/4/5 零次解析请求


def test_apk_risk_during_jitter_window_stops_next_resolve(tmp_path, monkeypatch):
    """P2 复现：并发下载时，下一集已通过熔断检查、正在错峰等待，此时
    上一集下载（含 APK sink 刷 URL）撞风控——等待结束后必须重查熔断，
    不能继续请求受保护接口。"""
    from xdl.application import usecases as uc_mod
    monkeypatch.setattr(uc_mod.random, "uniform", lambda a, b: 0.3)

    tracks = [AlbumTrack(str(i), f"第{i}集", i) for i in range(1, 3)]
    album = Album("123", "专辑", total=2, tracks=tracks)
    src = _OrderSource(album)
    src.max_consecutive_failures = 3

    class _RiskSink(FakeSink):
        def write(self, url, path, reporter, cancel=None, progress_sink=None,
                  expected_total=0):
            if url.endswith("/1.mp3"):
                raise RiskControlError("风控拦截", ret=3005)
            super().write(url, path, reporter, cancel, progress_sink,
                          expected_total)

    uc = DownloadAlbumUseCase(src, _RiskSink(), str(tmp_path), concurrency=2,
                              retry=RetryPolicy(max_attempts=1, backoff_base=0,
                                                cooldown=0, global_rounds=0))
    res = run(uc.execute("123", Quality.STANDARD))

    assert src.order == ["1"]     # 第 2 集在风控信号后零次解析
    assert res.risk_control is not None


def test_file_sink_stale_url_retries_with_re_resolve(tmp_path):
    """P2 复现：非 APK 通道（FileSink 无自刷新）遇失效链接时，下载重试
    必须重新解析拿新 URL（拆分前的行为），而不是对旧地址重试到底。"""
    class _UrlSource(FakeSource):
        """每次解析返回递增 URL：第 1 个是失效地址，第 2 个有效。"""

        def __init__(self, album):
            super().__init__(album)
            self.resolves = 0

        async def get_track(self, track_id):
            self.resolves += 1
            self.calls[track_id] = self.calls.get(track_id, 0) + 1
            url = f"http://x/{track_id}-v{self.resolves}.mp3"
            return Track(track_id=track_id, title=f"t{track_id}",
                         play_urls=[PlayUrl("MP3_64", url)])

    class _StaleUrlSink(FakeSink):
        def write(self, url, path, reporter, cancel=None, progress_sink=None,
                  expected_total=0):
            if "-v1" in url:
                raise NetworkError("HTTP 403: 链接已失效")
            super().write(url, path, reporter, cancel, progress_sink,
                          expected_total)

    src = _UrlSource(_one_track_album())
    uc = DownloadAlbumUseCase(src, _StaleUrlSink(), str(tmp_path), retry=FAST)
    res = run(uc.execute("123", Quality.STANDARD))

    assert len(res.downloaded) == 1 and not res.failed
    assert src.calls["1"] == 2     # 首次解析 + 重试时重新解析


def test_apk_download_stage_login_required_terminates_batch(tmp_path):
    """下载阶段登录态失效：终止批次并 requeue，剩余任务可恢复。"""
    tracks = [AlbumTrack(str(i), f"第{i}集", i) for i in range(1, 6)]
    album = Album("123", "专辑", total=5, tracks=tracks)
    src = _OrderSource(album)
    src.max_consecutive_failures = 3

    class _LoginSink(FakeSink):
        def write(self, url, path, reporter, cancel=None, progress_sink=None,
                  expected_total=0):
            if url.endswith("/1.mp3"):
                super().write(url, path, reporter, cancel, progress_sink,
                              expected_total)
                return
            from xdl.errors import LoginRequiredError
            raise LoginRequiredError("登录态已失效")

    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        uc = DownloadAlbumUseCase(src, _LoginSink(), str(tmp_path / "downloads"),
                                  concurrency=1, retry=FAST, store=store)
        with pytest.raises(Exception, match="登录态已失效"):
            run(uc.execute("123", Quality.STANDARD))
        rows = _db_rows(db)
        states = {row["track_id"]: row["state"] for row in rows}
        assert states["1"] == "done"
        assert states["2"] == "pending"     # requeue 待恢复
        assert src.order == ["1", "2"]
    finally:
        store.close()


def test_apk_download_failures_do_not_count_consecutive(tmp_path):
    """匿名 CDN 下载连续失败不计入熔断：走普通逐项失败，不触发
    ConsecutiveFailureError（熔断保护的是受保护解析端点的配额）。"""
    tracks = [AlbumTrack(str(i), f"第{i}集", i) for i in range(1, 6)]
    album = Album("123", "专辑", total=5, tracks=tracks)
    src = _OrderSource(album)
    src.max_consecutive_failures = 3

    uc = DownloadAlbumUseCase(src, _FailSink(), str(tmp_path), concurrency=1,
                              retry=RetryPolicy(max_attempts=1, backoff_base=0,
                                                cooldown=0, global_rounds=0))
    res = run(uc.execute("123", Quality.STANDARD))

    assert len(res.failed) == 5 and not res.downloaded
    assert all(src.calls[str(i)] == 1 for i in range(1, 6))


def test_apk_download_stop_does_not_hang(tmp_path):
    """下载中请求停止：不悬挂，未完成项保留待恢复。"""
    import threading

    tracks = [AlbumTrack(str(i), f"第{i}集", i) for i in range(1, 6)]
    album = Album("123", "专辑", total=5, tracks=tracks)
    src = _OrderSource(album)
    src.max_consecutive_failures = 3
    gate = threading.Event()

    class _GatedCancelSink(FakeSink):
        def write(self, url, path, reporter, cancel=None, progress_sink=None,
                  expected_total=0):
            self.writes.append((url, path))
            gate.wait(timeout=5)
            raise CancelledByUser("stop")

    sink = _GatedCancelSink()

    async def scenario():
        stop = asyncio.Event()
        uc = DownloadAlbumUseCase(src, sink, str(tmp_path), concurrency=2,
                                  retry=FAST, stop_event=stop)
        task = asyncio.create_task(uc.execute("123", Quality.STANDARD))
        await asyncio.sleep(0.5)
        stop.set()
        gate.set()
        return await asyncio.wait_for(task, timeout=5)

    res = run(scenario())
    assert not res.downloaded and not res.failed


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


def test_resume_risk_control_stops_later_quality_groups(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        store.save_album_meta("123", "专辑", 2)
        store.save_album_meta("456", "另一专辑", 1)
        store.upsert_pending([
            DownloadTask("1", "123", "第1集", Quality.STANDARD.value, 1),
            DownloadTask("2", "123", "第2集", Quality.HIGH.value, 2),
            DownloadTask("3", "456", "第1集", Quality.STANDARD.value, 1),
        ])

        src = FakeSource(behavior={
            "1": [RiskControlError("系统繁忙", ret=3005)],
        })
        uc = ResumeUseCase(src, FakeSink(), str(tmp_path / "downloads"),
                           store, concurrency=1, retry=FAST)

        results = run(uc.execute())

        assert len(results) == 1
        assert results[0].failed == []
        assert results[0].risk_control == "系统繁忙"
        assert results[0].deferred == 3
        assert src.calls == {"1": 1}
        assert [row["state"] for row in _db_rows(db)] == [
            "failed", "pending", "pending",
        ]
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


# ---- 风控后自动轮询恢复 ----


def test_risk_poll_wait_for_is_bounded_and_growing():
    policy = RiskRecoveryPolicy(initial_wait=10, backoff_factor=2, max_wait=25)
    waits = [policy.wait_for(i) for i in (1, 2, 3, 4, 5)]
    assert waits[0] >= 10 and waits[0] < 11
    assert waits[1] >= 20 and waits[1] < 22
    assert all(w >= 25 for w in waits[2:])
    assert RiskRecoveryPolicy(initial_wait=0).wait_for(1) == 0.0


def test_album_risk_poll_recovers_and_continues(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        src = FakeSource(_one_track_album(), behavior={
            "1": [RiskControlError("系统繁忙", ret=3005), "ok", "ok"],
        })
        policy = RiskRecoveryPolicy(enabled=True, initial_wait=0, max_duration=0)
        uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path / "downloads"),
                                  retry=FAST, store=store,
                                  risk_recovery=policy)

        res = run(uc.execute("123", Quality.STANDARD))

        assert len(res.downloaded) == 1 and not res.failed
        assert res.risk_control is None
        assert res.recovered is True
        assert res.risk_wait_seconds == 0.0
        # 1 次风控 + 1 次探针 + 1 次恢复后重跑
        assert src.calls["1"] == 3
        assert [r["state"] for r in _db_rows(db)] == ["done"]
        assert store.pending_tasks("123") == []
    finally:
        store.close()


def test_album_risk_poll_multiple_rounds(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        src = FakeSource(_one_track_album(), behavior={
            "1": [RiskControlError("a", ret=3005),
                  RiskControlError("b", ret=3005), "ok", "ok", "ok"],
        })
        policy = RiskRecoveryPolicy(enabled=True, initial_wait=0, max_duration=0)
        uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path / "downloads"),
                                  retry=FAST, store=store,
                                  risk_recovery=policy)

        res = run(uc.execute("123", Quality.STANDARD))

        assert len(res.downloaded) == 1 and not res.failed
        assert res.recovered is True
        # 批次风控 1 次 + 探针 2 次（第 1 次仍风控）+ 恢复后重跑 1 次
        assert src.calls["1"] == 4
    finally:
        store.close()


def test_album_risk_poll_timeout_keeps_deferred(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        src = FakeSource(_one_track_album(), behavior={
            "1": [RiskControlError("系统繁忙", ret=3005)],
        })
        policy = RiskRecoveryPolicy(enabled=True, initial_wait=0,
                                    max_duration=0.01)
        uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path / "downloads"),
                                  retry=FAST, store=store,
                                  risk_recovery=policy)

        res = run(uc.execute("123", Quality.STANDARD))

        assert res.risk_control is not None
        assert res.deferred == 1
        assert res.recovered is False
        # 触发项保持 FAILED(retryable)，供人工 resume 恢复
        rows = _db_rows(db)
        assert rows[0]["state"] == "failed"
        assert rows[0]["retryable"] == 1
    finally:
        store.close()


def test_album_risk_poll_cancel_requeues_tasks(tmp_path, monkeypatch):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        src = FakeSource(_one_track_album(), behavior={
            "1": [RiskControlError("系统繁忙", ret=3005)],
        })
        # 去掉 worker 的随机错峰，让风控立即发生，再验证「等待中被停止」
        monkeypatch.setattr("xdl.application.usecases.random.uniform",
                            lambda _lo, _hi: 0.0)
        policy = RiskRecoveryPolicy(enabled=True, initial_wait=2,
                                    max_duration=0)

        async def scenario():
            stop = asyncio.Event()
            uc = DownloadAlbumUseCase(
                src, FakeSink(), str(tmp_path / "downloads"),
                retry=FAST, store=store, stop_event=stop,
                risk_recovery=policy,
            )

            async def _cancel_later():
                await asyncio.sleep(0.05)
                stop.set()

            asyncio.create_task(_cancel_later())
            return await uc.execute("123", Quality.STANDARD)

        res = run(scenario())

        assert res.risk_control is not None
        assert res.stopped is False       # stopped 由 Facade 层标记
        # 等待中被停止：任务应回 PENDING，供 resume 继续
        rows = _db_rows(db)
        assert rows[0]["state"] == "pending"
    finally:
        store.close()


def test_album_risk_poll_only_recovers_risk_items(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        album = Album("123", "专辑", total=2, tracks=[
            AlbumTrack(track_id="1", title="第1集", index=1),
            AlbumTrack(track_id="2", title="第2集", index=2),
        ])
        src = FakeSource(album, behavior={
            "1": [AuthError("无权访问")],
            "2": [RiskControlError("系统繁忙", ret=3005), "ok", "ok"],
        })
        policy = RiskRecoveryPolicy(enabled=True, initial_wait=0, max_duration=0)
        uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path / "downloads"),
                                  retry=FAST, store=store, concurrency=1,
                                  risk_recovery=policy)

        res = run(uc.execute("123", Quality.STANDARD))

        assert len(res.downloaded) == 1
        assert [track.track_id for track, _err in res.failed] == ["1"]
        assert res.risk_control is None
        assert res.recovered is True
        assert src.calls["2"] == 3
    finally:
        store.close()


def test_album_risk_poll_disabled_preserves_old_behavior(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        src = FakeSource(_one_track_album(), behavior={
            "1": [RiskControlError("系统繁忙", ret=3005)],
        })
        uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path / "downloads"),
                                  retry=FAST, store=store,
                                  risk_recovery=RiskRecoveryPolicy())
        res = run(uc.execute("123", Quality.STANDARD))

        assert res.risk_control is not None
        assert res.deferred == 1
        assert res.recovered is False
        assert src.calls["1"] == 1
    finally:
        store.close()


def test_track_risk_poll_recovers(tmp_path):
    src = FakeSource(behavior={
        "1": [RiskControlError("系统繁忙", ret=3005), "ok", "ok"],
    })
    policy = RiskRecoveryPolicy(enabled=True, initial_wait=0, max_duration=0)
    uc = DownloadTrackUseCase(src, FakeSink(), str(tmp_path), retry=FAST,
                              risk_recovery=policy)

    path = run(uc.execute("1", Quality.STANDARD))

    assert path.endswith(".mp3")
    assert src.calls["1"] == 3


def test_track_risk_poll_disabled_raises_immediately(tmp_path):
    src = FakeSource(behavior={"1": [RiskControlError("系统繁忙", ret=3005)]})
    uc = DownloadTrackUseCase(src, FakeSink(), str(tmp_path), retry=FAST,
                              risk_recovery=RiskRecoveryPolicy())
    with pytest.raises(RiskControlError):
        run(uc.execute("1", Quality.STANDARD))
    assert src.calls["1"] == 1


def test_track_risk_poll_timeout_fails_retryable(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        src = FakeSource(behavior={
            "1": [RiskControlError("系统繁忙", ret=3005)],
        })
        policy = RiskRecoveryPolicy(enabled=True, initial_wait=0,
                                    max_duration=0.01)
        uc = DownloadTrackUseCase(src, FakeSink(), str(tmp_path), retry=FAST,
                                  store=store, risk_recovery=policy)
        with pytest.raises(RiskControlError):
            run(uc.execute("1", Quality.STANDARD))
        # 解析在任务入库之前失败：任务库不受影响（与现有单曲行为一致）
        assert _db_rows(db) == []
    finally:
        store.close()


def test_resume_risk_poll_recovers_and_continues_next_album(tmp_path):
    db = tmp_path / "tasks.db"
    store = SqliteTaskStore(str(db))
    try:
        store.save_album_meta("123", "专辑A", 1)
        store.save_album_meta("456", "专辑B", 1)
        store.upsert_pending([
            DownloadTask("1", "123", "第1集", Quality.STANDARD.value, 1),
            DownloadTask("2", "456", "第1集", Quality.STANDARD.value, 1),
        ])
        src = FakeSource(behavior={
            "1": [RiskControlError("系统繁忙", ret=3005), "ok", "ok"],
        })
        policy = RiskRecoveryPolicy(enabled=True, initial_wait=0, max_duration=0)
        uc = ResumeUseCase(src, FakeSink(), str(tmp_path / "downloads"),
                           store, concurrency=1, retry=FAST,
                           risk_recovery=policy)

        results = run(uc.execute())

        assert len(results) == 2
        assert results[0].recovered is True
        assert results[0].risk_control is None
        assert len(results[0].downloaded) == 1
        assert len(results[1].downloaded) == 1
        assert [r["state"] for r in _db_rows(db)] == ["done", "done"]
    finally:
        store.close()
