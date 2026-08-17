# -*- coding: utf-8 -*-
"""Web 前端的单实例任务运行器。"""
from __future__ import annotations

import copy
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone

from ..application import Facade
from ..application.diagnostics import (
    extract_device_identity,
    generate_signatures,
    login_cache_status,
    refresh_login_cookies,
)
from ..application.usecases import AlbumResult
from ..config import paths
from ..config import platform as platform_conf
from ..domain import DownloadTask, TaskState, parse_range
from ..errors import CancelledByUser, XdlError
from ..risk import summarize_risk_events
from ..settings import Settings
from .web_config import (load_web_settings, save_web_settings,
                         settings_dict)


# 单次删除/恢复请求接受的最大 id 数。够覆盖任何真实任务库，又能挡住畸形请求。
_MAX_SELECTION = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_ids(ids) -> list[int]:
    """去重并校验 id 列表；超过上限直接拒绝而不是悄悄截断。"""
    cleaned = list(dict.fromkeys(int(i) for i in (ids or [])))
    if len(cleaned) > _MAX_SELECTION:
        raise ValueError(
            f"一次最多操作 {_MAX_SELECTION} 条任务，本次收到 {len(cleaned)} 条。"
        )
    return cleaned


def _rederive_browser_paths(values: dict, old_settings: Settings) -> None:
    """切换 browser 时，让"程序自动填的"路径跟随新浏览器重派生。

    WebUI 表单全量提交，无法用"字段是否在提交里"判断用户改没改路径，只能按值
    判断：本次未编辑、且旧值仍等于自动派生结果（或为空）的字段，置空后交给
    Settings.__post_init__ 按新 browser 重新派生；用户自定义路径一律保留。

    覆盖身份三件套（Profile / Cookie 缓存 / 设备指纹）与浏览器可执行文件路径。
    漏掉任何一件都会让新浏览器沿用上一个浏览器的会话或指纹。
    """
    if (values.get("browser") or "auto") == (old_settings.browser or "auto"):
        return
    old_chrome_path = str(old_settings.chrome_path or "")
    if (values.get("chrome_path") == old_settings.chrome_path
            and (not old_chrome_path
                 or platform_conf.is_known_browser_path(old_chrome_path))):
        values["chrome_path"] = ""
    for field in paths.DERIVED_PATH_BUILDERS:
        old_value = getattr(old_settings, field, "")
        if (values.get(field) == old_value
                and paths.is_derived_path(field, old_value)):
            values[field] = ""


class OperationBusyError(RuntimeError):
    pass


class OperationNotCancellableError(RuntimeError):
    pass


class WebProgressReporter:
    def __init__(self, runtime: "WebRuntime", operation_id: str):
        self._runtime = runtime
        self._operation_id = operation_id

    def start(self, title: str, total: int) -> None:
        self._runtime._report_progress(
            self._operation_id, title=title, done=0, total=total,
        )

    def update(self, done: int, total: int) -> None:
        self._runtime._report_progress(
            self._operation_id, done=done, total=total,
        )

    def finish(self, path: str) -> None:
        self._runtime._append_note(self._operation_id, f"已保存: {path}")

    def note(self, msg: str) -> None:
        self._runtime._append_note(self._operation_id, msg)


class WebRuntime:
    """串行运行会接触 Source/Chrome/任务库的操作，并暴露可轮询快照。"""

    def __init__(self, settings: Settings | None = None, *, facade=None,
                 facade_factory: Callable[[Settings], object] = Facade.from_config,
                 settings_path: str | None = None,
                 persist_settings: bool = True):
        self._settings_path = settings_path
        self._persist_settings = persist_settings
        self._settings = settings or load_web_settings(settings_path)
        self._facade_factory = facade_factory
        self._facade = facade or facade_factory(self._settings)
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._operation: dict | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    def bootstrap(self) -> dict:
        tasks = self.tasks_snapshot()
        login = (self._facade.auth_status()
                 if self._settings.source_backend == "apk"
                 else login_cache_status(self._settings))
        if self._settings.source_backend == "apk":
            login = {**login, "browser": "", "browser_name": "APK",
                     "cache_exists": bool(login.get("authenticated")),
                     "profile_exists": True,
                     "other_browser_authenticated": None}
        return {
            "settings": settings_dict(self._settings),
            "login": login,
            "operation": self.operation_snapshot(include_result=False),
            "tasks": tasks["tasks"],
            "counts": tasks["counts"],
            "page": tasks["page"],
            "task_error": tasks.get("error"),
        }

    def operation_snapshot(self, *, include_result: bool = True) -> dict | None:
        with self._lock:
            snapshot = copy.deepcopy(self._operation)
        if snapshot is not None and not include_result:
            snapshot["has_result"] = snapshot.get("result") is not None
            snapshot.pop("result", None)
        return snapshot

    def tasks_snapshot(self, *, state: str | None = None,
                       search: str = "", album_id: str = "",
                       limit: int = 100, offset: int = 0) -> dict:
        try:
            task_state = TaskState(state) if state else None
            result = self._facade.query_tasks(
                state=task_state, search=search, album_id=album_id,
                limit=limit, offset=offset,
            )
            tasks = [_task_dict(task) for task in result.tasks]
            counts = {
                "all": sum(result.counts.values()),
                **{
                    task_state.value: result.counts.get(task_state, 0)
                    for task_state in TaskState
                },
            }
            return {
                "tasks": tasks,
                "counts": counts,
                "page": {
                    "offset": result.offset,
                    "limit": result.limit,
                    "total": result.total,
                    "has_previous": result.offset > 0,
                    "has_next": result.offset + len(tasks) < result.total,
                },
            }
        except XdlError as exc:
            return {
                "tasks": [],
                "counts": _task_counts([]),
                "page": {
                    "offset": 0, "limit": limit, "total": 0,
                    "has_previous": False, "has_next": False,
                },
                "error": str(exc),
            }

    def task_ids(self, *, state: str | None = None, search: str = "",
                 album_id: str = "", cap: int = _MAX_SELECTION) -> dict:
        """「选中全部」用的 id 快照。

        返回 truncated 而不是静默截断：前端得知道自己拿到的是不是完整集合，
        否则确认框上的数字会撒谎。
        """
        task_state = TaskState(state) if state else None
        ids = self._facade.query_task_ids(
            state=task_state, search=search, album_id=album_id, cap=cap,
        )
        return {"ids": ids, "count": len(ids), "truncated": len(ids) >= cap}

    def preview_tasks(self, ids: list[int]) -> dict:
        """删除前的后果预览。由后端按真实 id 集统计，跨页选择也不会撒谎。"""
        return {"summary": asdict(self._facade.summarize_tasks(_clean_ids(ids)))}

    def delete_tasks(self, ids: list[int]) -> dict:
        """删除任务并清理 .part；不占用操作锁，下载进行中也能用。"""
        ids = _clean_ids(ids)
        result = self._facade.delete_tasks(ids)
        return {"result": asdict(result), **self.tasks_snapshot()}

    def requeue_tasks(self, ids: list[int]) -> dict:
        """把选中的失败任务放回待恢复队列（不自动开跑下载）。"""
        ids = _clean_ids(ids)
        return {"requeued": self._facade.requeue_tasks(ids),
                **self.tasks_snapshot()}

    def risk_report(self) -> dict:
        return {
            "path": self._settings.risk_log_path,
            "summary": summarize_risk_events(self._settings.risk_log_path),
        }

    def start_login(self) -> dict:
        def run(reporter, _cancel):
            reporter.note("正在打开浏览器，请在浏览器中完成登录。")
            path = self._facade.login()
            return {"profile_path": path}
        return self._start("login", "登录", False, run)

    def apk_auth_status(self) -> dict:
        return self._facade.auth_status()

    def apk_login_config(self) -> dict:
        return self._facade.login_config()

    def apk_send_sms(self, mobile: str, fds_otp: dict) -> dict:
        return self._facade.send_login_sms(mobile, fds_otp)

    def apk_verify_sms(self, code: str) -> dict:
        return self._apk_auth_mutation(self._facade.verify_login_sms, code)

    def apk_login_password(self, account: str, password: str, mode: str,
                           fds_otp: dict) -> dict:
        return self._apk_auth_mutation(
            self._facade.login_password, account, password, mode, fds_otp,
        )

    def apk_logout(self) -> dict:
        return self._apk_auth_mutation(self._facade.logout)

    def apk_switch_account(self, uid: str) -> dict:
        return self._apk_auth_mutation(self._facade.switch_account, uid)

    def apk_delete_account(self, uid: str) -> dict:
        return self._apk_auth_mutation(self._facade.delete_account, uid)

    def _apk_auth_mutation(self, method: Callable, *args) -> dict:
        with self._lock:
            if self._operation and self._operation["status"] == "running":
                raise OperationBusyError(
                    "有下载或恢复操作正在运行，请停止或等待完成后再切换 APK 账号。"
                )
            return method(*args)

    def start_download(self, *, mode: str, target: str,
                       quality: str | None = None,
                       range_: str | None = None) -> dict:
        target = target.strip()
        if not target:
            raise ValueError("请输入专辑或曲目链接 / ID。")
        if mode not in {"track", "album"}:
            raise ValueError("下载类型必须是 track 或 album。")
        if quality not in {None, "high", "standard", "low"}:
            raise ValueError("音质必须是 high、standard 或 low。")
        if mode == "album":
            parse_range(range_)

        def run(reporter, cancel):
            if mode == "track":
                path = self._facade.download_track(
                    target, quality=quality, reporter=reporter, cancel=cancel,
                )
                return {"mode": mode, "path": path}
            result = self._facade.download_album(
                target, quality=quality, range_=range_,
                reporter=reporter, cancel=cancel,
            )
            return {"mode": mode, "album": _album_result_dict(result)}

        label = "下载单曲" if mode == "track" else "下载专辑"
        return self._start(f"download_{mode}", label, True, run)

    def start_resume(self) -> dict:
        def run(reporter, cancel):
            results = self._facade.resume(reporter=reporter, cancel=cancel)
            return {"albums": [_album_result_dict(result) for result in results]}
        return self._start("resume", "恢复任务", True, run)

    def start_formats(self, target: str) -> dict:
        target = target.strip()
        if not target:
            raise ValueError("请输入曲目链接或 ID。")
        return self._start(
            "formats", "探测音质", False,
            lambda _reporter, _cancel: self._facade.list_formats(target),
        )

    def start_inspect_storage(self) -> dict:
        return self._start(
            "inspect_storage", "检查浏览器存储", False,
            lambda _reporter, _cancel: self._facade.inspect_storage(),
        )

    def start_gen_sign(self, *, device_info_path: str | None = None,
                       repeat: int = 1) -> dict:
        return self._start(
            "gen_sign", "签名冒烟", False,
            lambda _reporter, _cancel: generate_signatures(
                device_info_path, repeat,
            ),
        )

    def start_extract_device(self, *, output: str | None = None,
                             profile: str | None = None,
                             headless: bool = True,
                             refresh: bool = False,
                             fresh_profile: bool = False) -> dict:
        return self._start(
            "extract_device", "采集设备信息", False,
            lambda _reporter, _cancel: extract_device_identity(
                self._settings, output=output, profile=profile,
                headless=headless, refresh=refresh,
                fresh_profile=fresh_profile,
            ),
        )

    def start_refresh_cookies(self, *, headless: bool = True) -> dict:
        return self._start(
            "refresh_cookies", "刷新登录凭据", False,
            lambda _reporter, _cancel: refresh_login_cookies(
                self._settings, headless=headless,
            ),
        )

    def request_stop(self) -> dict:
        with self._lock:
            if not self._operation or self._operation["status"] != "running":
                raise OperationNotCancellableError("当前没有正在运行的任务。")
            if not self._operation["cancellable"]:
                raise OperationNotCancellableError("当前操作不能安全停止，请等待完成。")
            self._cancel.set()
            self._operation["stop_requested"] = True
            self._append_note_locked("已请求优雅停止，正在保存进度。")
            return copy.deepcopy(self._operation)

    def update_settings(self, changes: dict) -> dict:
        with self._lock:
            if self._operation and self._operation["status"] == "running":
                raise OperationBusyError("有操作正在运行，完成或停止后才能修改设置。")
            values = asdict(self._settings)
            unknown = set(changes) - set(values)
            if unknown:
                raise ValueError(f"未知设置项: {', '.join(sorted(unknown))}")
            values.update(changes)
            _rederive_browser_paths(values, self._settings)
            new_settings = Settings(**values)
            new_facade = self._facade_factory(new_settings)
            if self._persist_settings:
                save_web_settings(new_settings, self._settings_path)
            old_facade = self._facade
            self._settings = new_settings
            self._facade = new_facade
            close = getattr(old_facade, "close", None)
            if close is not None:
                close()
            return settings_dict(self._settings)

    def open_downloads(self, task_id: int | None = None) -> dict:
        path = self._settings.download_dir
        if task_id is not None:
            task = next((row for row in self._facade.all_tasks()
                         if row.id == task_id), None)
            if task is None:
                raise ValueError(f"任务不存在: {task_id}")
            candidate = task.target_path or task.part_path
            if candidate:
                path = os.path.dirname(candidate)
        path = os.path.abspath(path)
        os.makedirs(path, exist_ok=True)
        _open_directory(path)
        return {"path": path}

    def wait(self, timeout: float = 5.0) -> dict | None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.operation_snapshot()

    def shutdown(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._cancel.set()
            thread.join(5)
        if thread is None or not thread.is_alive():
            close = getattr(self._facade, "close", None)
            if close is not None:
                close()

    def _start(self, kind: str, label: str, cancellable: bool,
               runner: Callable) -> dict:
        with self._lock:
            if self._operation and self._operation["status"] == "running":
                raise OperationBusyError(
                    f"“{self._operation['label']}”正在运行，请先等待或停止。"
                )
            operation_id = uuid.uuid4().hex
            self._cancel = threading.Event()
            self._operation = {
                "id": operation_id,
                "kind": kind,
                "label": label,
                "status": "running",
                "cancellable": cancellable,
                "stop_requested": False,
                "started_at": _now(),
                "finished_at": None,
                "current_title": "",
                "progress_done": 0,
                "progress_total": 0,
                "notes": [],
                "message": "",
                "error_category": None,
                "result": None,
            }
            self._thread = threading.Thread(
                target=self._run_operation,
                args=(operation_id, runner),
                name=f"xdl-web-{kind}", daemon=True,
            )
            self._thread.start()
            return copy.deepcopy(self._operation)

    def _run_operation(self, operation_id: str, runner: Callable) -> None:
        reporter = WebProgressReporter(self, operation_id)
        try:
            result = runner(reporter, self._cancel)
            stopped = self._cancel.is_set() or _result_stopped(result)
            with self._lock:
                if not self._is_current(operation_id):
                    return
                self._operation["status"] = "stopped" if stopped else "succeeded"
                self._operation["message"] = (
                    "已停止，进度可以继续恢复。" if stopped else "操作已完成。"
                )
                self._operation["result"] = result
                self._operation["finished_at"] = _now()
        except CancelledByUser as exc:
            self._finish_error(operation_id, "stopped", exc)
        except XdlError as exc:
            self._finish_error(operation_id, "failed", exc, exc.category)
        except Exception as exc:  # noqa: BLE001 - 后台线程必须形成可见失败态
            self._finish_error(operation_id, "failed", exc, "unexpected")

    def _finish_error(self, operation_id: str, status: str, exc: Exception,
                      category: str | None = None) -> None:
        with self._lock:
            if not self._is_current(operation_id):
                return
            self._operation["status"] = status
            self._operation["message"] = str(exc)
            self._operation["error_category"] = category
            self._operation["finished_at"] = _now()

    def _report_progress(self, operation_id: str, *, title: str | None = None,
                         done: int | None = None,
                         total: int | None = None) -> None:
        with self._lock:
            if not self._is_current(operation_id):
                return
            if title is not None:
                self._operation["current_title"] = title
            if done is not None:
                self._operation["progress_done"] = max(0, int(done))
            if total is not None:
                self._operation["progress_total"] = max(0, int(total))

    def _append_note(self, operation_id: str, message: str) -> None:
        with self._lock:
            if self._is_current(operation_id):
                self._append_note_locked(message)

    def _append_note_locked(self, message: str) -> None:
        notes = self._operation["notes"]
        notes.append({"timestamp": _now(), "message": str(message)})
        del notes[:-100]

    def _is_current(self, operation_id: str) -> bool:
        return bool(self._operation and self._operation["id"] == operation_id)


def _task_dict(task: DownloadTask) -> dict:
    total = max(0, task.total_bytes)
    done = max(0, task.bytes_done)
    progress = 100 if task.state is TaskState.DONE else (
        min(100, done * 100 // total) if total else 0
    )
    return {
        "id": task.id,
        "track_id": task.track_id,
        "album_id": task.album_id,
        "title": task.title,
        "quality": task.quality,
        "album_index": task.album_index,
        "state": task.state.value,
        "target_path": task.target_path,
        "part_path": task.part_path,
        "total_bytes": total,
        "bytes_done": done,
        "progress": progress,
        "attempts": task.attempts,
        "last_error_code": task.last_error_code,
        "last_error_msg": task.last_error_msg,
    }


def _task_counts(tasks: list[dict]) -> dict:
    counts = {"all": len(tasks), "pending": 0, "downloading": 0,
              "done": 0, "failed": 0}
    for task in tasks:
        state = task["state"]
        if state in counts:
            counts[state] += 1
    return counts


def _album_result_dict(result: AlbumResult) -> dict:
    return {
        "album_title": result.album_title,
        "downloaded": list(result.downloaded),
        "skipped": list(result.skipped),
        "failed": [
            {"index": track.index, "track_id": track.track_id,
             "title": track.title, "error": error}
            for track, error in result.failed
        ],
        "incomplete": result.incomplete,
        "stopped": result.stopped,
        "risk_control": result.risk_control,
        "deferred": result.deferred,
        "recovered": result.recovered,
        "risk_wait_seconds": result.risk_wait_seconds,
        "summary": result.summary(),
    }


def _result_stopped(result) -> bool:
    if not isinstance(result, dict):
        return False
    album = result.get("album")
    if isinstance(album, dict) and album.get("stopped"):
        return True
    albums = result.get("albums")
    return bool(isinstance(albums, list)
                and any(item.get("stopped") for item in albums))


def _open_directory(path: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif os.name == "nt":  # pragma: no cover - Windows only
        os.startfile(path)  # type: ignore[attr-defined]
    else:  # pragma: no cover - Linux only
        subprocess.Popen(["xdg-open", path])
