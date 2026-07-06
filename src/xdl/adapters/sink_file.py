# -*- coding: utf-8 -*-
"""文件输出适配器（实现 MediaSink 端口，见 docs/architecture.md §8.2）。

MVP：流式下载到 .part 临时文件，完成后原子重命名为最终文件（崩溃安全）。
字节级 Range 续传留待任务引擎阶段接入。
"""
from __future__ import annotations

import os
import re

import requests

from ..config import platform
from ..errors import CancelledByUser, NetworkError

_PROGRESS_STEP = 1024 * 1024
_META_SUFFIX = ".meta"
_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")
_CONTENT_RANGE_TOTAL_RE = re.compile(r"bytes\s+\*/(\d+)")


def _content_range(headers) -> tuple[int, int, int] | None:
    raw = headers.get("Content-Range", "")
    m = _CONTENT_RANGE_RE.match(raw)
    if not m or m.group(3) == "*":
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _content_range_total(headers) -> int:
    raw = headers.get("Content-Range", "")
    m = _CONTENT_RANGE_TOTAL_RE.match(raw)
    return int(m.group(1)) if m else 0


class FileSink:
    def __init__(self, http_timeout: int = 60):
        self._timeout = http_timeout

    def write(self, url: str, target_path: str, reporter, cancel=None,
              progress_sink=None, expected_total: int = 0) -> None:
        os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
        part_path = target_path + ".part"
        base_headers = {"User-Agent": platform.UA, "Referer": platform.REFERER}
        try:
            self._write_stream(url, target_path, part_path, base_headers,
                               reporter, cancel, progress_sink, expected_total)
        except requests.RequestException as e:
            raise NetworkError(f"下载失败: {e}") from e

    def _write_stream(self, url, target_path, part_path, base_headers, reporter,
                      cancel, progress_sink, expected_total) -> None:
        resume_from = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        validator = self._read_validator(part_path) if resume_from > 0 else None
        if resume_from > 0 and validator is None:
            self._discard_part(part_path)
            resume_from = 0
        use_range = resume_from > 0 and validator is not None

        while True:
            self._raise_if_cancelled(cancel)
            headers = dict(base_headers)
            if use_range:
                headers["Range"] = f"bytes={resume_from}-"
                headers["If-Range"] = validator
            with requests.get(url, headers=headers, stream=True,
                              timeout=self._timeout) as r:
                if use_range and r.status_code == 416:
                    total = _content_range_total(r.headers) or expected_total
                    if total and resume_from == total:
                        self._finish_existing_part(part_path, target_path, reporter,
                                                   progress_sink, total)
                        return
                    self._discard_part(part_path)
                    resume_from = 0
                    validator = None
                    use_range = False
                    continue

                if use_range and r.status_code == 206:
                    parsed = _content_range(r.headers)
                    if parsed is None or parsed[0] != resume_from:
                        self._discard_part(part_path)
                        resume_from = 0
                        validator = None
                        use_range = False
                        continue
                    total = parsed[2]
                    if expected_total and total and total != expected_total:
                        self._discard_part(part_path)
                        resume_from = 0
                        validator = None
                        use_range = False
                        continue
                    self._stream_response(r, part_path, target_path, "ab",
                                          resume_from, total, reporter, cancel,
                                          progress_sink)
                    return

                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0))
                self._write_validator(part_path, r.headers)
                self._stream_response(r, part_path, target_path, "wb", 0, total,
                                      reporter, cancel, progress_sink)
                return

    def _stream_response(self, response, part_path, target_path, mode, done, total,
                         reporter, cancel, progress_sink) -> None:
        last_persisted = done
        if reporter:
            reporter.start(os.path.basename(target_path), total)
            if done:
                reporter.update(done, total)
        with open(part_path, mode) as f:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                self._raise_if_cancelled(cancel)
                f.write(chunk)
                done += len(chunk)
                if reporter:
                    reporter.update(done, total)
                if progress_sink and done - last_persisted >= _PROGRESS_STEP:
                    progress_sink(done, total)
                    last_persisted = done
        if progress_sink:
            progress_sink(done, total)
        os.replace(part_path, target_path)   # 原子落盘
        self._discard_meta(part_path)
        if reporter:
            reporter.finish(target_path)

    def _finish_existing_part(self, part_path, target_path, reporter, progress_sink,
                              total) -> None:
        if progress_sink:
            progress_sink(total, total)
        os.replace(part_path, target_path)
        self._discard_meta(part_path)
        if reporter:
            reporter.finish(target_path)

    def _discard_part(self, part_path) -> None:
        try:
            os.remove(part_path)
        except FileNotFoundError:
            pass
        self._discard_meta(part_path)

    def _discard_meta(self, part_path) -> None:
        try:
            os.remove(part_path + _META_SUFFIX)
        except FileNotFoundError:
            pass

    def _read_validator(self, part_path) -> str | None:
        try:
            with open(part_path + _META_SUFFIX, encoding="utf-8") as f:
                return f.read().strip() or None
        except FileNotFoundError:
            return None

    def _write_validator(self, part_path, headers) -> None:
        validator = headers.get("ETag") or headers.get("Last-Modified")
        if not validator:
            self._discard_meta(part_path)
            return
        meta_path = part_path + _META_SUFFIX
        tmp_path = meta_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(validator)
        os.replace(tmp_path, meta_path)

    def _raise_if_cancelled(self, cancel) -> None:
        if cancel is not None and cancel.is_set():
            raise CancelledByUser("用户请求停止下载。")
