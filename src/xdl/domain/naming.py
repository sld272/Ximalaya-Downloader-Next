# -*- coding: utf-8 -*-
"""文件命名策略（见 docs/architecture.md §5）。

放在领域层，保证 CLI / 未来 GUI 等各前端命名一致。
"""
from __future__ import annotations

import re

_ILLEGAL = re.compile(r'[/\\:*?"<>|]')


class NamingPolicy:
    """把标题转成安全文件名。"""

    @staticmethod
    def sanitize(title: str) -> str:
        return _ILLEGAL.sub("_", title).strip() or "untitled"

    @classmethod
    def track_filename(cls, title: str, ext: str, index: int | None = None,
                       index_width: int = 0) -> str:
        """生成曲目文件名；index 用于专辑场景的序号补零。"""
        name = cls.sanitize(title)
        if index is not None:
            prefix = str(index).zfill(index_width) if index_width else str(index)
            name = f"{prefix} {name}"
        return f"{name}{ext}"
