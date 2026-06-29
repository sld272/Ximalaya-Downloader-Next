# -*- coding: utf-8 -*-
"""用例层单测：错误分级退避重试 + 失败收尾轮（用替身，零真实 I/O）。"""
import os

import pytest

from xdl.domain import Album, AlbumTrack, Track, PlayUrl, Quality
from xdl.application.usecases import (DownloadTrackUseCase, DownloadAlbumUseCase,
                                      RetryPolicy)
from xdl.errors import NetworkError, AuthError, ApiError

# 退避/冷却/间隔全置 0，测试不真正 sleep
FAST = RetryPolicy(max_attempts=3, backoff_base=0, cooldown=0, global_rounds=2)


class FakeSink:
    def write(self, url, path, reporter):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()


class FakeSource:
    """behavior: track_id -> 结果序列；元素为异常实例（抛出）或 'ok'（返回 Track）。
    最后一个元素会被重复使用。calls 记录每个 track 的调用次数。"""
    def __init__(self, album=None, behavior=None):
        self.album = album
        self.behavior = behavior or {}
        self.calls: dict[str, int] = {}

    def open(self): pass
    def close(self): pass
    def get_album(self, album_id): return self.album

    def get_track(self, track_id):
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
    path = uc.execute("1", Quality.STANDARD)
    assert path.endswith(".mp3")
    assert src.calls["1"] == 3       # 失败两次后第三次成功


def test_track_non_retryable_no_retry(tmp_path):
    src = FakeSource(behavior={"1": [AuthError("无权")]})
    uc = DownloadTrackUseCase(src, FakeSink(), str(tmp_path), retry=FAST)
    with pytest.raises(AuthError):
        uc.execute("1", Quality.STANDARD)
    assert src.calls["1"] == 1       # 不可重试，只试一次


def test_track_retryable_exhausted(tmp_path):
    src = FakeSource(behavior={"1": [NetworkError("x")]})   # 恒失败
    uc = DownloadTrackUseCase(src, FakeSink(), str(tmp_path),
                              retry=RetryPolicy(max_attempts=2, backoff_base=0))
    with pytest.raises(NetworkError):
        uc.execute("1", Quality.STANDARD)
    assert src.calls["1"] == 2       # 用满 max_attempts


def _one_track_album():
    return Album("123", "专辑", total=1,
                 tracks=[AlbumTrack(track_id="1", title="第1集", index=1)])


def test_album_recovery_round_salvages(tmp_path):
    # max_attempts=2 → 主轮 2 次失败；收尾轮再 2 次，第 4 次成功
    src = FakeSource(_one_track_album(), behavior={
        "1": [NetworkError("1"), NetworkError("2"), NetworkError("3"), "ok"]})
    uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path), request_interval=(0, 0),
                              retry=RetryPolicy(max_attempts=2, backoff_base=0,
                                                cooldown=0, global_rounds=2))
    res = uc.execute("123", Quality.STANDARD)
    assert len(res.downloaded) == 1 and not res.failed
    assert src.calls["1"] == 4


def test_album_non_retryable_stays_failed(tmp_path):
    src = FakeSource(_one_track_album(), behavior={"1": [AuthError("无权")]})
    uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path), request_interval=(0, 0),
                              retry=FAST)
    res = uc.execute("123", Quality.STANDARD)
    assert len(res.failed) == 1
    assert src.calls["1"] == 1       # 不可重试：主轮一次，收尾轮跳过


def test_album_rate_limit_is_retryable(tmp_path):
    # ret=1001 标记为 retryable，应被收尾轮重试并最终成功
    src = FakeSource(_one_track_album(), behavior={
        "1": [ApiError("风控", ret=1001, retryable=True), "ok"]})
    uc = DownloadAlbumUseCase(src, FakeSink(), str(tmp_path), request_interval=(0, 0),
                              retry=RetryPolicy(max_attempts=1, backoff_base=0,
                                                cooldown=0, global_rounds=2))
    res = uc.execute("123", Quality.STANDARD)
    assert len(res.downloaded) == 1 and not res.failed
    assert src.calls["1"] == 2       # 主轮 1 次失败，收尾轮第 2 次成功
