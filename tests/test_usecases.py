# -*- coding: utf-8 -*-
"""用例层单测：错误分级退避重试 + 失败收尾轮 + 并发（用替身，零真实 I/O）。"""
import asyncio
import os

import pytest

from xdl.domain import Album, AlbumTrack, Track, PlayUrl, Quality
from xdl.application.usecases import (DownloadTrackUseCase, DownloadAlbumUseCase,
                                      RetryPolicy)
from xdl.errors import NetworkError, AuthError, ApiError

# 退避/冷却全置 0，测试不真正 sleep
FAST = RetryPolicy(max_attempts=3, backoff_base=0, cooldown=0, global_rounds=2)


def run(coro):
    return asyncio.run(coro)


class FakeSink:
    def write(self, url, path, reporter):   # 同步，用例里经 to_thread 调用
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()


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
