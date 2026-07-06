# -*- coding: utf-8 -*-
"""TUI 前端冒烟测试（需要 textual，未安装则跳过）。"""
import asyncio

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable  # noqa: E402

from xdl.domain import DownloadTask, TaskState  # noqa: E402
from xdl.frontends.tui import XdlApp, _progress, _short  # noqa: E402
from xdl.settings import Settings  # noqa: E402


class FakeFacade:
    def __init__(self, tasks=None):
        self._tasks = tasks or []
        self.stopped_flag = False

    def all_tasks(self):
        return self._tasks


def _task(track_id, index, state, bytes_done=0, total=0):
    return DownloadTask(
        track_id=track_id, album_id="123", title=f"第{index}集",
        quality="standard", album_index=index, state=state,
        bytes_done=bytes_done, total_bytes=total,
    )


def test_progress_and_short():
    assert _progress(_task("1", 1, TaskState.DONE)) == "100%"
    assert _progress(_task("1", 1, TaskState.DOWNLOADING, 5, 10)) == "50%"
    assert _progress(_task("1", 1, TaskState.PENDING)) == "-"
    assert _short("x" * 30, 10) == "x" * 9 + "…"
    assert _short("abc", 10) == "abc"


def test_tui_boots_and_lists_tasks():
    async def go():
        tasks = [_task("1", 1, TaskState.DONE),
                 _task("2", 2, TaskState.DOWNLOADING, 3, 6)]
        app = XdlApp(FakeFacade(tasks), Settings())
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#tasks", DataTable)
            assert len(table.columns) == 5
            assert table.row_count == 2
    asyncio.run(go())


def test_album_button_requires_target_and_stays_idle():
    async def go():
        app = XdlApp(FakeFacade(), Settings())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#album")
            await pilot.pause()
            assert app._busy is False          # 空 target 不应启动任务
    asyncio.run(go())
