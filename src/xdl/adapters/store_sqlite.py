# -*- coding: utf-8 -*-
"""SQLite 任务库适配器。"""
from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from ..domain import DownloadTask, TaskState

_RETRYABLE_ERROR_CODES = ("network", "sign", "api", "storage", "cancelled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


_MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_migrate_v1]


class SqliteTaskStore:
    """TaskStore 的 SQLite 实现。"""

    def __init__(self, path: str):
        self._path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._migrate_locked()

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
                    continue
                if row:
                    self._conn.execute(
                        """
                        UPDATE download_task
                        SET album_id=?, title=?, album_index=?, state=?,
                            target_path=CASE WHEN ?='' THEN target_path ELSE ? END,
                            part_path=CASE WHEN ?='' THEN part_path ELSE ? END,
                            last_error_code='', last_error_msg='', updated_at=?
                        WHERE id=?
                        """,
                        (task.album_id, task.title, task.album_index,
                         TaskState.PENDING.value,
                         task.target_path, task.target_path,
                         task.part_path, task.part_path,
                         now, row["id"]),
                    )
                    ids.append(int(row["id"]))
                else:
                    cur = self._conn.execute(
                        """
                        INSERT INTO download_task(
                          track_id, album_id, title, quality, album_index, state,
                          target_path, part_path, total_bytes, bytes_done, attempts,
                          last_error_code, last_error_msg, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (task.track_id, task.album_id, task.title, task.quality,
                         task.album_index, TaskState.PENDING.value,
                         task.target_path, task.part_path, task.total_bytes,
                         task.bytes_done, task.attempts, task.last_error_code,
                         task.last_error_msg, now, now),
                    )
                    ids.append(int(cur.lastrowid))
        return self._tasks_by_ids(ids)

    def mark_downloading(self, task_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE download_task
                SET state=?, attempts=attempts+1, updated_at=?
                WHERE id=?
                """,
                (TaskState.DOWNLOADING.value, _now(), task_id),
            )

    def mark_done(self, task_id: int, target_path: str) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE download_task
                SET state=?, target_path=?, part_path='', last_error_code='',
                    last_error_msg='', bytes_done=CASE
                      WHEN total_bytes > 0 THEN total_bytes ELSE bytes_done END,
                    updated_at=?, completed_at=?
                WHERE id=?
                """,
                (TaskState.DONE.value, target_path, now, now, task_id),
            )

    def mark_failed(self, task_id: int, category: str, msg: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE download_task
                SET state=?, last_error_code=?, last_error_msg=?, updated_at=?
                WHERE id=?
                """,
                (TaskState.FAILED.value, category, msg, _now(), task_id),
            )

    def record_progress(self, task_id: int, bytes_done: int, total: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE download_task
                SET bytes_done=?, total_bytes=CASE WHEN ? > 0 THEN ? ELSE total_bytes END,
                    updated_at=?
                WHERE id=?
                """,
                (max(0, bytes_done), total, total, _now(), task_id),
            )

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

    def requeue_retryable_failed(self) -> int:
        marks = ",".join("?" for _ in _RETRYABLE_ERROR_CODES)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"""
                UPDATE download_task
                SET state=?, updated_at=?
                WHERE state=? AND last_error_code IN ({marks})
                """,
                (TaskState.PENDING.value, _now(), TaskState.FAILED.value,
                 *_RETRYABLE_ERROR_CODES),
            )
            return int(cur.rowcount)

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

    def album_cursor(self, album_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT cursor FROM album_sync WHERE album_id=?",
                (album_id,),
            ).fetchone()
        return str(row["cursor"]) if row else ""

    def close(self) -> None:
        with self._lock:
            self._conn.close()

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
            total_bytes=int(row["total_bytes"]),
            bytes_done=int(row["bytes_done"]),
            attempts=int(row["attempts"]),
            last_error_code=str(row["last_error_code"]),
            last_error_msg=str(row["last_error_msg"]),
        )
