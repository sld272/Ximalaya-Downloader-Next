# -*- coding: utf-8 -*-
"""SQLite 任务库适配器。"""
from __future__ import annotations

import errno
import functools
import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from ..domain import DownloadTask, TaskState
from ..errors import StorageError

try:  # pragma: no cover - exercised on Unix/macOS in tests via monkeypatch.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:  # pragma: no cover - Windows fallback.
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _wrap_sqlite_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except sqlite3.Error as e:
            raise StorageError(f"任务库操作失败: {e}") from e
    return wrapper


def _lock_file(f) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
    if msvcrt is not None:  # pragma: no cover
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
    return True


def _unlock_file(f) -> None:
    if fcntl is not None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


class _TaskDbLock:
    def __init__(self, db_path: str):
        self._path = os.path.join(os.path.dirname(os.path.abspath(db_path)), "xdl.lock")
        self._file = None

    def acquire(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        try:
            self._file = open(self._path, "a+b")
        except OSError as e:
            raise StorageError(f"任务库锁不可用: {e}") from e
        try:
            if not _lock_file(self._file):
                raise StorageError("已有 xdl 实例正在运行，请等待其结束后再恢复任务。")
        except Exception:
            self._file.close()
            self._file = None
            raise

    def release(self) -> None:
        if self._file is None:
            return
        try:
            _unlock_file(self._file)
        finally:
            self._file.close()
            self._file = None


def _migrate_v1(conn: sqlite3.Connection) -> None:
    conn.executescript("""
CREATE TABLE IF NOT EXISTS download_task(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  track_id TEXT NOT NULL,
  album_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  quality TEXT NOT NULL,
  album_index INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'pending',
  target_path TEXT NOT NULL DEFAULT '',
  part_path TEXT NOT NULL DEFAULT '',
  total_bytes INTEGER NOT NULL DEFAULT 0,
  bytes_done INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error_code TEXT NOT NULL DEFAULT '',
  last_error_msg TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(track_id, quality)
);
CREATE INDEX IF NOT EXISTS idx_task_album ON download_task(album_id, state);

CREATE TABLE IF NOT EXISTS album_sync(
  album_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  total_known INTEGER NOT NULL DEFAULT 0,
  cursor TEXT NOT NULL DEFAULT '',
  last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
""")


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _migrate_v2(conn: sqlite3.Connection) -> None:
    if not _has_column(conn, "download_task", "retryable"):
        conn.execute(
            "ALTER TABLE download_task "
            "ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0"
        )
    if not _has_column(conn, "download_task", "index_width"):
        conn.execute(
            "ALTER TABLE download_task "
            "ADD COLUMN index_width INTEGER NOT NULL DEFAULT 0"
        )


_MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_migrate_v1, _migrate_v2]


class SqliteTaskStore:
    """TaskStore 的 SQLite 实现。"""

    def __init__(self, path: str):
        self._path = path
        self._db_lock: _TaskDbLock | None = None
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        if path != ":memory:":
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            except OSError as e:
                raise StorageError(f"任务库目录不可用: {e}") from e
            self._db_lock = _TaskDbLock(path)
            self._db_lock.acquire()
        try:
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
                self._migrate_locked()
        except sqlite3.Error as e:
            if self._db_lock is not None:
                self._db_lock.release()
            raise StorageError(f"任务库不可用: {e}") from e
        except Exception:
            if self._db_lock is not None:
                self._db_lock.release()
            raise

    @_wrap_sqlite_errors
    def upsert_pending(self, tasks: list[DownloadTask]) -> list[DownloadTask]:
        if not tasks:
            return []
        now = _now()
        ids: list[int] = []
        with self._lock, self._conn:
            for task in tasks:
                row = self._conn.execute(
                    "SELECT * FROM download_task WHERE track_id=? AND quality=?",
                    (task.track_id, task.quality),
                ).fetchone()
                if row and row["state"] == TaskState.DONE.value:
                    ids.append(int(row["id"]))
                    continue
                if row:
                    self._conn.execute(
                        """
                        UPDATE download_task
                        SET album_id=?, title=?, album_index=?, state=?,
                            target_path=CASE WHEN ?='' THEN target_path ELSE ? END,
                            part_path=CASE WHEN ?='' THEN part_path ELSE ? END,
                            index_width=CASE WHEN ? > 0 THEN ? ELSE index_width END,
                            last_error_code='', last_error_msg='', retryable=0,
                            updated_at=?
                        WHERE id=?
                        """,
                        (task.album_id, task.title, task.album_index,
                         TaskState.PENDING.value,
                         task.target_path, task.target_path,
                         task.part_path, task.part_path,
                         task.index_width, task.index_width,
                         now, row["id"]),
                    )
                    ids.append(int(row["id"]))
                else:
                    cur = self._conn.execute(
                        """
                        INSERT INTO download_task(
                          track_id, album_id, title, quality, album_index, state,
                          target_path, part_path, index_width, total_bytes, bytes_done,
                          attempts, last_error_code, last_error_msg, retryable,
                          created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (task.track_id, task.album_id, task.title, task.quality,
                         task.album_index, TaskState.PENDING.value,
                         task.target_path, task.part_path, task.index_width,
                         task.total_bytes, task.bytes_done, task.attempts,
                         task.last_error_code, task.last_error_msg, 0, now, now),
                    )
                    ids.append(int(cur.lastrowid))
        return self._tasks_by_ids(ids)

    @_wrap_sqlite_errors
    def mark_downloading(self, task_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE download_task
                SET state=?, attempts=attempts+1, retryable=0, updated_at=?
                WHERE id=? AND state IN (?, ?, ?)
                """,
                (TaskState.DOWNLOADING.value, _now(), task_id,
                 TaskState.PENDING.value, TaskState.DOWNLOADING.value,
                 TaskState.DONE.value),
            )

    @_wrap_sqlite_errors
    def mark_done(self, task_id: int, target_path: str) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE download_task
                SET state=?, target_path=?, part_path='', last_error_code='',
                    last_error_msg='', retryable=0, bytes_done=CASE
                      WHEN total_bytes > 0 THEN total_bytes ELSE bytes_done END,
                    updated_at=?, completed_at=?
                WHERE id=? AND state IN (?, ?, ?)
                """,
                (TaskState.DONE.value, target_path, now, now, task_id,
                 TaskState.PENDING.value, TaskState.DOWNLOADING.value,
                 TaskState.DONE.value),
            )

    @_wrap_sqlite_errors
    def mark_failed(self, task_id: int, category: str, msg: str,
                    retryable: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE download_task
                SET state=?, last_error_code=?, last_error_msg=?, retryable=?,
                    updated_at=?
                WHERE id=? AND state IN (?, ?, ?)
                """,
                (TaskState.FAILED.value, category, msg, 1 if retryable else 0,
                 _now(), task_id, TaskState.PENDING.value,
                 TaskState.DOWNLOADING.value, TaskState.FAILED.value),
            )

    @_wrap_sqlite_errors
    def record_progress(self, task_id: int, bytes_done: int, total: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE download_task
                SET bytes_done=?, total_bytes=CASE WHEN ? > 0 THEN ? ELSE total_bytes END,
                    updated_at=?
                WHERE id=? AND state=?
                """,
                (max(0, bytes_done), total, total, _now(), task_id,
                 TaskState.DOWNLOADING.value),
            )

    @_wrap_sqlite_errors
    def requeue_stale(self) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE download_task
                SET state=?, updated_at=?
                WHERE state=?
                """,
                (TaskState.PENDING.value, _now(), TaskState.DOWNLOADING.value),
            )
            return int(cur.rowcount)

    @_wrap_sqlite_errors
    def requeue_retryable_failed(self) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE download_task
                SET state=?, retryable=0, updated_at=?
                WHERE state=? AND retryable=1
                """,
                (TaskState.PENDING.value, _now(), TaskState.FAILED.value),
            )
            return int(cur.rowcount)

    @_wrap_sqlite_errors
    def pending_albums(self) -> list[tuple[str, str, int]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT d.album_id AS album_id,
                       COALESCE(NULLIF(a.title, ''), d.album_id) AS title,
                       COUNT(*) AS pending_count
                FROM download_task d
                LEFT JOIN album_sync a ON a.album_id = d.album_id
                WHERE d.album_id != '' AND d.state IN (?, ?)
                GROUP BY d.album_id, COALESCE(NULLIF(a.title, ''), d.album_id)
                ORDER BY MIN(d.id)
                """,
                (TaskState.PENDING.value, TaskState.DOWNLOADING.value),
            ).fetchall()
        return [(str(r["album_id"]), str(r["title"]), int(r["pending_count"]))
                for r in rows]

    @_wrap_sqlite_errors
    def pending_tasks(self, album_id: str) -> list[DownloadTask]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM download_task
                WHERE album_id=? AND state=?
                ORDER BY album_index, id
                """,
                (album_id, TaskState.PENDING.value),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    @_wrap_sqlite_errors
    def all_tasks(self) -> list[DownloadTask]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM download_task ORDER BY album_id, album_index, id"
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    @_wrap_sqlite_errors
    def save_album_meta(self, album_id: str, title: str, total: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO album_sync(album_id, title, total_known)
                VALUES (?, ?, ?)
                ON CONFLICT(album_id) DO UPDATE SET
                  title=excluded.title,
                  total_known=excluded.total_known
                """,
                (album_id, title, total),
            )

    @_wrap_sqlite_errors
    def save_album_cursor(self, album_id: str, cursor: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO album_sync(album_id, cursor, last_synced_at)
                VALUES (?, ?, ?)
                ON CONFLICT(album_id) DO UPDATE SET
                  cursor=excluded.cursor,
                  last_synced_at=excluded.last_synced_at
                """,
                (album_id, cursor, _now()),
            )

    @_wrap_sqlite_errors
    def album_cursor(self, album_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT cursor FROM album_sync WHERE album_id=?",
                (album_id,),
            ).fetchone()
        return str(row["cursor"]) if row else ""

    @_wrap_sqlite_errors
    def album_total(self, album_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT total_known FROM album_sync WHERE album_id=?",
                (album_id,),
            ).fetchone()
        return int(row["total_known"]) if row else 0

    @_wrap_sqlite_errors
    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        finally:
            if self._db_lock is not None:
                self._db_lock.release()

    def _migrate_locked(self) -> None:
        version = self._schema_version_locked()
        for idx, migration in enumerate(_MIGRATIONS[version:], start=version + 1):
            with self._conn:
                migration(self._conn)
                self._conn.execute(
                    """
                    INSERT INTO meta(key, value) VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (str(idx),),
                )

    def _schema_version_locked(self) -> int:
        exists = self._conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='meta'
            """
        ).fetchone()
        if not exists:
            return 0
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def _tasks_by_ids(self, ids: list[int]) -> list[DownloadTask]:
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM download_task WHERE id IN ({marks}) ORDER BY id",
                ids,
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def _row_to_task(self, row: sqlite3.Row) -> DownloadTask:
        return DownloadTask(
            id=int(row["id"]),
            track_id=str(row["track_id"]),
            album_id=str(row["album_id"]),
            title=str(row["title"]),
            quality=str(row["quality"]),
            album_index=int(row["album_index"]),
            state=TaskState(str(row["state"])),
            target_path=str(row["target_path"]),
            part_path=str(row["part_path"]),
            index_width=int(row["index_width"]),
            total_bytes=int(row["total_bytes"]),
            bytes_done=int(row["bytes_done"]),
            attempts=int(row["attempts"]),
            last_error_code=str(row["last_error_code"]),
            last_error_msg=str(row["last_error_msg"]),
        )
