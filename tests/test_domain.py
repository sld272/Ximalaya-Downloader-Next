# -*- coding: utf-8 -*-
"""领域逻辑单测：音质协商、命名、ID/区间解析、专辑区间筛选。"""
import pytest

from xdl.domain import (Quality, PlayUrl, Track, Album, AlbumTrack,
                        NamingPolicy, parse_track_id, parse_album_id, parse_range)


def test_quality_negotiation_by_bitrate_and_codec():
    # 真实付费内容的档位：high=M4A_64(同码率取AAC), standard=MP3_64(次优), low=M4A_24(最低)
    real = ["M4A_64", "MP3_64", "MP3_32", "M4A_24"]
    assert Quality.HIGH.negotiate(real) == "M4A_64"
    assert Quality.STANDARD.negotiate(real) == "MP3_64"
    assert Quality.LOW.negotiate(real) == "M4A_24"
    # 码率优先于编码：128 高于 64，不因编码降级
    assert Quality.HIGH.negotiate(["MP3_64", "M4A_128"]) == "M4A_128"
    assert Quality.LOW.negotiate(["MP3_64", "M4A_128"]) == "MP3_64"
    # 单条 / 空
    assert Quality.HIGH.negotiate(["MP3_32"]) == "MP3_32"
    assert Quality.STANDARD.negotiate([]) is None


def test_track_select():
    t = Track("1", "标题", [
        PlayUrl("MP3_32", "u32"),
        PlayUrl("M4A_128", "u128"),
    ])
    assert t.select(Quality.HIGH).type == "M4A_128"
    assert t.select(Quality.LOW).type == "MP3_32"


def test_playurl_ext():
    assert PlayUrl("M4A_128", "x.m4a").ext == ".m4a"
    assert PlayUrl("MP3_64", "x.mp3").ext == ".mp3"


@pytest.mark.parametrize("raw,expected", [
    ("123456", "123456"),
    ("https://www.ximalaya.com/sound/789", "789"),
    ("https://www.ximalaya.com/youshengshu/123/456", "456"),
    ("  42  ", "42"),
])
def test_parse_track_id(raw, expected):
    assert parse_track_id(raw) == expected


def test_parse_track_id_invalid():
    with pytest.raises(ValueError):
        parse_track_id("no-digits-here")


def test_naming_sanitize():
    assert NamingPolicy.sanitize('a/b:c*?"<>|') == "a_b_c______"
    assert NamingPolicy.track_filename("标题", ".mp3", index=3, index_width=3) == "003 标题.mp3"


@pytest.mark.parametrize("raw,expected", [
    ("47517749", "47517749"),
    ("https://www.ximalaya.com/album/47517749", "47517749"),
    ("https://www.ximalaya.com/youshengshu/47517749/", "47517749"),
    ("  123  ", "123"),
])
def test_parse_album_id(raw, expected):
    assert parse_album_id(raw) == expected


def test_parse_album_id_invalid():
    with pytest.raises(ValueError):
        parse_album_id("no-digits")


@pytest.mark.parametrize("raw,expected", [
    (None, (None, None)),
    ("", (None, None)),
    ("1-20", (1, 20)),
    ("5-", (5, None)),
    ("-10", (None, 10)),
    ("7", (7, 7)),
    (" 3 - 8 ", (3, 8)),
])
def test_parse_range(raw, expected):
    assert parse_range(raw) == expected


@pytest.mark.parametrize("raw", ["20-1", "abc", "1-2-3"])
def test_parse_range_invalid(raw):
    with pytest.raises(ValueError):
        parse_range(raw)


def _album():
    tracks = [AlbumTrack(track_id=str(i), title=f"第{i}集", index=i) for i in range(1, 6)]
    return Album(album_id="a", title="专辑", total=5, tracks=tracks)


def test_album_select_range():
    al = _album()
    assert [t.index for t in al.select_range(2, 4)] == [2, 3, 4]
    assert [t.index for t in al.select_range(3, None)] == [3, 4, 5]
    assert [t.index for t in al.select_range(None, 2)] == [1, 2]
    assert [t.index for t in al.select_range(None, None)] == [1, 2, 3, 4, 5]


def test_album_is_complete():
    al = _album()
    assert al.is_complete
    al.total = 100
    assert not al.is_complete
