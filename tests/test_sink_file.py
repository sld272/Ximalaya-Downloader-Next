# -*- coding: utf-8 -*-
"""FileSink 字节级续传测试。"""
import requests

from xdl.adapters.sink_file import FileSink


def _write_meta(part, validator='"v1"'):
    (part.parent / (part.name + ".meta")).write_text(validator, encoding="utf-8")


class FakeResponse:
    def __init__(self, status_code, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size=0):
        yield from self._chunks


def test_file_sink_resumes_with_206(monkeypatch, tmp_path):
    target = tmp_path / "a.mp3"
    part = tmp_path / "a.mp3.part"
    part.write_bytes(b"abc")
    _write_meta(part)
    calls = []

    def fake_get(url, headers, stream, timeout):
        calls.append(headers.copy())
        assert headers["Range"] == "bytes=3-"
        assert headers["If-Range"] == '"v1"'
        return FakeResponse(
            206,
            {"Content-Range": "bytes 3-5/6"},
            [b"de", b"f"],
        )

    monkeypatch.setattr("xdl.adapters.sink_file.requests.get", fake_get)
    progress = []
    FileSink().write("http://x/a.mp3", str(target), None,
                     progress_sink=lambda done, total: progress.append((done, total)),
                     expected_total=6)

    assert target.read_bytes() == b"abcdef"
    assert not part.exists()
    assert not (tmp_path / "a.mp3.part.meta").exists()
    assert calls and calls[0]["Range"] == "bytes=3-"
    assert progress[-1] == (6, 6)


def test_file_sink_falls_back_to_full_download_on_200(monkeypatch, tmp_path):
    target = tmp_path / "a.mp3"
    part = tmp_path / "a.mp3.part"
    part.write_bytes(b"old")
    _write_meta(part)

    def fake_get(url, headers, stream, timeout):
        assert headers["Range"] == "bytes=3-"
        assert headers["If-Range"] == '"v1"'
        return FakeResponse(200, {"Content-Length": "3"}, [b"new"])

    monkeypatch.setattr("xdl.adapters.sink_file.requests.get", fake_get)
    FileSink().write("http://x/a.mp3", str(target), None)

    assert target.read_bytes() == b"new"
    assert not part.exists()
    assert not (tmp_path / "a.mp3.part.meta").exists()


def test_file_sink_discards_part_without_validator(monkeypatch, tmp_path):
    target = tmp_path / "a.mp3"
    part = tmp_path / "a.mp3.part"
    part.write_bytes(b"old")
    calls = []

    def fake_get(url, headers, stream, timeout):
        calls.append(headers.copy())
        assert "Range" not in headers
        assert "If-Range" not in headers
        return FakeResponse(200, {"Content-Length": "3", "ETag": '"v2"'}, [b"new"])

    monkeypatch.setattr("xdl.adapters.sink_file.requests.get", fake_get)
    FileSink().write("http://x/a.mp3", str(target), None)

    assert target.read_bytes() == b"new"
    assert not part.exists()
    assert not (tmp_path / "a.mp3.part.meta").exists()
    assert len(calls) == 1


def test_file_sink_discards_part_when_total_mismatches(monkeypatch, tmp_path):
    target = tmp_path / "a.mp3"
    part = tmp_path / "a.mp3.part"
    part.write_bytes(b"abc")
    _write_meta(part)
    ranges = []

    def fake_get(url, headers, stream, timeout):
        ranges.append(headers.get("Range", ""))
        if headers.get("Range"):
            return FakeResponse(206, {"Content-Range": "bytes 3-5/999"}, [b"def"])
        return FakeResponse(200, {"Content-Length": "6"}, [b"abcdef"])

    monkeypatch.setattr("xdl.adapters.sink_file.requests.get", fake_get)
    FileSink().write("http://x/a.mp3", str(target), None, expected_total=6)

    assert ranges == ["bytes=3-", ""]
    assert target.read_bytes() == b"abcdef"
    assert not part.exists()


def test_file_sink_treats_416_as_complete_when_part_is_full(monkeypatch, tmp_path):
    target = tmp_path / "a.mp3"
    part = tmp_path / "a.mp3.part"
    part.write_bytes(b"abcdef")
    _write_meta(part)

    def fake_get(url, headers, stream, timeout):
        assert headers["Range"] == "bytes=6-"
        return FakeResponse(416, {"Content-Range": "bytes */6"})

    monkeypatch.setattr("xdl.adapters.sink_file.requests.get", fake_get)
    progress = []
    FileSink().write("http://x/a.mp3", str(target), None,
                     progress_sink=lambda done, total: progress.append((done, total)))

    assert target.read_bytes() == b"abcdef"
    assert not part.exists()
    assert not (tmp_path / "a.mp3.part.meta").exists()
    assert progress == [(6, 6)]


def test_file_sink_discards_oversized_part_after_416(monkeypatch, tmp_path):
    target = tmp_path / "a.mp3"
    part = tmp_path / "a.mp3.part"
    part.write_bytes(b"abcdef")
    _write_meta(part)
    ranges = []

    def fake_get(url, headers, stream, timeout):
        ranges.append(headers.get("Range", ""))
        if headers.get("Range"):
            return FakeResponse(416, {"Content-Range": "bytes */3"})
        return FakeResponse(200, {"Content-Length": "3"}, [b"new"])

    monkeypatch.setattr("xdl.adapters.sink_file.requests.get", fake_get)
    FileSink().write("http://x/a.mp3", str(target), None)

    assert ranges == ["bytes=6-", ""]
    assert target.read_bytes() == b"new"
    assert not part.exists()
    assert not (tmp_path / "a.mp3.part.meta").exists()
