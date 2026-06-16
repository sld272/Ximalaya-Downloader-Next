# -*- coding: utf-8 -*-
"""文件型 CookieJar（实现 CookieJar 端口）。

把登录会话以 Playwright storage_state（JSON）形式持久化到本地。
"""
from __future__ import annotations

import os


class FileCookieJar:
    def __init__(self, path: str):
        self._path = path

    def state_path(self) -> str | None:
        return self._path if os.path.exists(self._path) else None

    def location(self) -> str:
        return self._path

    def ensure_dir(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
