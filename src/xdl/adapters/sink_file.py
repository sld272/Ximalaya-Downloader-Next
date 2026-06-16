# -*- coding: utf-8 -*-
"""文件输出适配器（实现 MediaSink 端口，见 docs/architecture.md §8.2）。

MVP：流式下载到 .part 临时文件，完成后原子重命名为最终文件（崩溃安全）。
字节级 Range 续传留待任务引擎阶段接入。
"""
from __future__ import annotations

import os

import requests

from ..config import platform
from ..errors import NetworkError


class FileSink:
    def __init__(self, http_timeout: int = 60):
        self._timeout = http_timeout

    def write(self, url: str, target_path: str, reporter) -> None:
        os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
        part_path = target_path + ".part"
        headers = {"User-Agent": platform.UA, "Referer": platform.REFERER}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=self._timeout) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0))
                done = 0
                if reporter:
                    reporter.start(os.path.basename(target_path), total)
                with open(part_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        if reporter:
                            reporter.update(done, total)
        except requests.RequestException as e:
            raise NetworkError(f"下载失败: {e}") from e

        os.replace(part_path, target_path)   # 原子落盘
        if reporter:
            reporter.finish(target_path)
