# -*- coding: utf-8 -*-
"""终端 UI 前端（薄壳，见 docs/architecture.md §11）。

与 CLI 平级：只负责收输入、画界面，业务全部走 `Facade`。
- 下载/恢复/登录跑在**线程 worker** 里（Facade 方法是同步阻塞的），不冻界面；
- 进度以两条通道呈现：`ProgressReporter.note` 事件流写日志区，任务表格则**轮询任务库**
  （M3 已把每集状态/字节数持久化），因此专辑并发下载也能看到逐集进度。
- “停止”按钮设置一个 threading.Event，由 Facade 桥接到优雅停止（等价 Ctrl-C）。
"""
from __future__ import annotations

import sys
import threading

from ..application import Facade
from ..domain import TaskState
from ..errors import XdlError
from ..settings import Settings

try:
    from textual import on, work
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.widgets import (Button, DataTable, Footer, Header, Input,
                                 RichLog, Select)
except ImportError as e:  # pragma: no cover - 仅在未装 textual 时触发
    raise SystemExit(
        "未安装 TUI 依赖。请先执行：pip install 'ximalaya-downloader-next[tui]'"
    ) from e


_STATE_ICON = {
    TaskState.PENDING: "…",
    TaskState.DOWNLOADING: "⏬",
    TaskState.DONE: "✓",
    TaskState.FAILED: "✗",
}


class _TuiReporter:
    """ProgressReporter 实现：把事件搬运到 UI 线程写进日志区。"""

    def __init__(self, app: "XdlApp"):
        self._app = app

    def start(self, title: str, total: int) -> None:
        self._app.call_from_thread(self._app.log_line, f"开始下载: {title}")

    def update(self, done: int, total: int) -> None:
        # 逐字节百分比由任务表轮询任务库呈现，这里不刷屏。
        pass

    def finish(self, path: str) -> None:
        self._app.call_from_thread(self._app.log_line, f"完成: {path}")

    def note(self, msg: str) -> None:
        self._app.call_from_thread(self._app.log_line, msg)


class XdlApp(App):
    """xdl 简易任务面板。"""

    TITLE = "喜马拉雅下载器"
    CSS = """
    #inputs { height: auto; padding: 0 1; }
    #inputs Input { width: 1fr; }
    #inputs #quality { width: 16; }
    #buttons { height: auto; padding: 0 1; }
    #buttons Button { margin: 0 1 0 0; }
    #tasks { height: 1fr; }
    #log { height: 30%; border: round $panel; padding: 0 1; }
    """
    BINDINGS = [("q", "quit", "退出")]

    def __init__(self, facade: Facade, settings: Settings):
        super().__init__()
        self._facade = facade
        self._settings = settings
        self._reporter = _TuiReporter(self)
        self._cancel = threading.Event()
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            with Horizontal(id="inputs"):
                yield Input(placeholder="专辑 / 曲目 链接或 ID", id="target")
                yield Select(
                    [("高", "high"), ("标准", "standard"), ("低", "low")],
                    value=self._settings.default_quality, allow_blank=False,
                    id="quality",
                )
                yield Input(placeholder="区间 如 1-20（可空）", id="range")
            with Horizontal(id="buttons"):
                yield Button("下载专辑", id="album", variant="primary")
                yield Button("下载单曲", id="track")
                yield Button("恢复", id="resume", variant="success")
                yield Button("停止", id="stop", variant="error")
                yield Button("登录", id="login")
        yield DataTable(id="tasks", zebra_stripes=True, cursor_type="row")
        yield RichLog(id="log", markup=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tasks", DataTable)
        table.add_columns("专辑", "#", "标题", "状态", "进度")
        self._set_stop_enabled(False)
        # 预热任务库：同进程共享一个 store，避免下载线程二次加文件锁失败。
        try:
            self._facade.all_tasks()
        except XdlError as e:
            self.log_line(f"[错误] 任务库不可用：{e}")
        self.set_interval(0.7, self.refresh_tasks)
        self.refresh_tasks()

    # ---- 展示 ----
    def log_line(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    def refresh_tasks(self) -> None:
        try:
            tasks = self._facade.all_tasks()
        except XdlError:
            return
        table = self.query_one("#tasks", DataTable)
        table.clear()
        for t in tasks:
            table.add_row(
                _short(t.album_id) or "单曲", str(t.album_index or "-"),
                _short(t.title, 40), _STATE_ICON.get(t.state, "?"), _progress(t),
            )

    # ---- 交互 ----
    @on(Button.Pressed)
    def _dispatch(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "stop":
            return self._request_stop()
        if bid == "login":
            return self._run_login()
        if self._busy:
            return self.log_line("有任务进行中，请先停止或等待完成。")
        if bid == "album":
            self._run_album()
        elif bid == "track":
            self._run_track()
        elif bid == "resume":
            self._run_resume()

    def _target(self) -> str:
        return self.query_one("#target", Input).value.strip()

    def _quality(self) -> str:
        return str(self.query_one("#quality", Select).value)

    def _range(self) -> str | None:
        return self.query_one("#range", Input).value.strip() or None

    def _request_stop(self) -> None:
        if not self._busy:
            return
        self._cancel.set()
        self.log_line("已请求停止，正在收尾…")

    def _begin(self, msg: str) -> None:
        self._busy = True
        self._cancel = threading.Event()
        self._set_stop_enabled(True)
        self.log_line(msg)

    def _on_done(self, summary: str) -> None:
        self._busy = False
        self._set_stop_enabled(False)
        self.log_line(summary)
        self.refresh_tasks()

    def _set_stop_enabled(self, running: bool) -> None:
        for bid in ("album", "track", "resume", "login"):
            self.query_one(f"#{bid}", Button).disabled = running
        self.query_one("#stop", Button).disabled = not running

    # ---- 线程 worker（Facade 同步方法在此阻塞运行）----
    def _run_album(self) -> None:
        target = self._target()
        if not target:
            return self.log_line("请先填入专辑链接或 ID。")
        self._begin(f"开始下载专辑：{target}")
        self._album_worker(target, self._quality(), self._range())

    def _run_track(self) -> None:
        target = self._target()
        if not target:
            return self.log_line("请先填入曲目链接或 ID。")
        self._begin(f"开始下载单曲：{target}")
        self._track_worker(target, self._quality())

    def _run_resume(self) -> None:
        self._begin("开始恢复未完成任务…")
        self._resume_worker()

    def _run_login(self) -> None:
        self.log_line("正在打开浏览器登录…（完成后回到本窗口）")
        self._login_worker()

    @work(thread=True, exclusive=True, group="job")
    def _album_worker(self, target: str, quality: str, range_: str | None) -> None:
        try:
            result = self._facade.download_album(
                target, quality=quality, range_=range_,
                reporter=self._reporter, cancel=self._cancel,
            )
            summary = result.summary() + ("（已停止）" if result.stopped else "")
            self.call_from_thread(self._on_done, summary)
        except XdlError as e:
            self.call_from_thread(self._on_done, f"[错误] {e}")
        except Exception as e:  # noqa: BLE001 - 兜底不让 worker 静默吞崩
            self.call_from_thread(self._on_done, f"[异常] {e}")

    @work(thread=True, exclusive=True, group="job")
    def _track_worker(self, target: str, quality: str) -> None:
        try:
            path = self._facade.download_track(
                target, quality=quality, reporter=self._reporter,
                cancel=self._cancel)
            self.call_from_thread(self._on_done, f"已保存: {path}")
        except XdlError as e:
            self.call_from_thread(self._on_done, f"[错误] {e}")
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._on_done, f"[异常] {e}")

    @work(thread=True, exclusive=True, group="job")
    def _resume_worker(self) -> None:
        try:
            results = self._facade.resume(
                reporter=self._reporter, cancel=self._cancel)
            if not results:
                return self.call_from_thread(self._on_done, "没有未完成任务。")
            stopped = any(r.stopped for r in results)
            summary = "；".join(r.summary() for r in results)
            self.call_from_thread(
                self._on_done, summary + ("（已停止）" if stopped else ""))
        except XdlError as e:
            self.call_from_thread(self._on_done, f"[错误] {e}")
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._on_done, f"[异常] {e}")

    @work(thread=True, exclusive=True, group="login")
    def _login_worker(self) -> None:
        try:
            path = self._facade.login()
            self.call_from_thread(self.log_line, f"登录态已保存：{path}")
        except XdlError as e:
            self.call_from_thread(self.log_line, f"[错误] {e}")
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.log_line, f"[异常] {e}")


def _short(text: str, width: int = 18) -> str:
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"


def _progress(task) -> str:
    if task.state is TaskState.DONE:
        return "100%"
    if task.total_bytes > 0:
        return f"{min(100, task.bytes_done * 100 // task.total_bytes)}%"
    if task.bytes_done > 0:
        return f"{task.bytes_done // 1024} KiB"
    return "-"


def main(argv: list[str] | None = None) -> int:
    settings = Settings()
    facade = Facade.from_config(settings)
    XdlApp(facade, settings).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
