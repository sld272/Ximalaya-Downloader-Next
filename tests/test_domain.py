# -*- coding: utf-8 -*-
"""领域逻辑单测：音质协商、命名、trackId 解析。"""
import pytest

from xdl.domain import Quality, PlayUrl, Track, NamingPolicy, parse_track_id


def test_quality_negotiation_prefers_then_falls_back():
    # STANDARD 偏好 MP3_64；不可用时回退
    assert Quality.STANDARD.negotiate(["M4A_128", "MP3_64"]) == "MP3_64"
    assert Quality.STANDARD.negotiate(["M4A_128", "MP3_32"]) == "M4A_128"
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
