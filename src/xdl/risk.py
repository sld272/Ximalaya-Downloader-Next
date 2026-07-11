# -*- coding: utf-8 -*-
"""风控观测事件。

只记录诊断所需的最小元数据；刻意不接受 Cookie、请求头、播放 URL 或设备指纹。
"""
from __future__ import annotations

import json
import os
import threading
from collections import Counter
from datetime import datetime, timezone


class RiskEventRecorder:
    """把受保护接口的结果追加为 JSONL，供离线统计。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def record(self, *, track_id: str, elapsed_ms: int, outcome: str,
               ret: int | None = None, msg: str | None = None,
               in_flight: int = 1, session_id: str | None = None,
               request_index: int | None = None,
               started_at: str | None = None,
               authenticated: bool | None = None) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "track_id": str(track_id),
            "elapsed_ms": int(elapsed_ms),
            "outcome": str(outcome),
            "ret": ret,
            "msg": msg,
            "in_flight": int(in_flight),
        }
        if session_id is not None:
            event["session_id"] = str(session_id)
        if request_index is not None:
            event["request_index"] = int(request_index)
        if started_at is not None:
            event["started_at"] = str(started_at)
        if authenticated is not None:
            event["authenticated"] = bool(authenticated)
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")


def summarize_risk_events(path: str) -> dict:
    """汇总已有观测，不发起任何网络请求。坏行被忽略。"""
    rows: list[dict] = []
    if not path or not os.path.exists(path):
        return {"total": 0, "outcomes": {}, "ret_counts": {},
                "first_risk_request_index": None, "recovery_seconds": [],
                "successes_before_first_risk": 0,
                "max_in_flight": 0, "latency_ms": {},
                "duration_seconds": 0.0, "requests_per_minute": 0.0,
                "peak_requests_per_minute": 0,
                "request_interval_seconds": {},
                "outcomes_by_in_flight": {},
                "outcomes_by_authentication": {},
                "latest_session": {}}
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(row, dict):
                rows.append(row)

    outcomes = Counter(str(row.get("outcome") or "unknown") for row in rows)
    ret_counts = Counter(str(row["ret"]) for row in rows if row.get("ret") is not None)
    first_risk_position = next((i for i, row in enumerate(rows)
                                if row.get("outcome") == "risk_control"), None)
    first_risk_row = (rows[first_risk_position]
                      if first_risk_position is not None else None)
    first_risk = None
    successes_before_first_risk = sum(row.get("outcome") == "success" for row in rows)
    if first_risk_row is not None:
        request_index = first_risk_row.get("request_index")
        session_id = first_risk_row.get("session_id")
        first_risk = int(request_index) if request_index is not None else first_risk_position + 1
        if request_index is not None and session_id is not None:
            successes_before_first_risk = sum(
                row.get("outcome") == "success"
                and row.get("session_id") == session_id
                and row.get("request_index") is not None
                and int(row["request_index"]) < int(request_index)
                for row in rows
            )
        else:
            successes_before_first_risk = sum(
                row.get("outcome") == "success"
                for row in rows[:first_risk_position]
            )
    recovery: list[float] = []
    for index, row in enumerate(rows):
        if row.get("outcome") != "risk_control":
            continue
        try:
            started = datetime.fromisoformat(row["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        for later in rows:
            if later.get("outcome") != "success":
                continue
            if later.get("authenticated") != row.get("authenticated"):
                continue
            try:
                ended = datetime.fromisoformat(
                    later.get("started_at") or later["timestamp"]
                )
            except (KeyError, TypeError, ValueError):
                continue
            if ended <= started:
                continue
            recovery.append(round((ended - started).total_seconds(), 3))
            break

    latencies = sorted(int(row.get("elapsed_ms") or 0) for row in rows)
    latency = {}
    if latencies:
        def percentile(fraction: float) -> int:
            return latencies[min(len(latencies) - 1,
                                 max(0, int((len(latencies) - 1) * fraction)))]
        latency = {"min": latencies[0], "p50": percentile(0.5),
                   "p95": percentile(0.95), "max": latencies[-1]}

    timestamps: list[datetime] = []
    for row in rows:
        try:
            timestamps.append(datetime.fromisoformat(
                row.get("started_at") or row["timestamp"]
            ))
        except (KeyError, TypeError, ValueError):
            continue
    timestamps.sort()
    duration = ((timestamps[-1] - timestamps[0]).total_seconds()
                if len(timestamps) >= 2 else 0.0)
    request_rate = round(len(timestamps) * 60 / duration, 3) if duration > 0 else 0.0
    intervals = [round((right - left).total_seconds(), 3)
                 for left, right in zip(timestamps, timestamps[1:])]
    interval_stats = {}
    if intervals:
        ordered = sorted(intervals)

        def interval_percentile(fraction: float) -> float:
            return ordered[min(len(ordered) - 1,
                               max(0, int((len(ordered) - 1) * fraction)))]
        interval_stats = {
            "min": ordered[0], "p50": interval_percentile(0.5),
            "p95": interval_percentile(0.95), "max": ordered[-1],
        }

    peak_per_minute = 0
    left = 0
    for right, current in enumerate(timestamps):
        while left <= right and (current - timestamps[left]).total_seconds() >= 60:
            left += 1
        peak_per_minute = max(peak_per_minute, right - left + 1)

    by_in_flight: dict[str, Counter] = {}
    by_authentication: dict[str, Counter] = {}
    for row in rows:
        key = str(int(row.get("in_flight") or 0))
        outcome = str(row.get("outcome") or "unknown")
        by_in_flight.setdefault(key, Counter())[outcome] += 1
        auth = row.get("authenticated")
        auth_key = "unknown" if auth is None else str(bool(auth)).lower()
        by_authentication.setdefault(auth_key, Counter())[outcome] += 1

    latest_id = rows[-1].get("session_id") or "legacy"
    latest_rows = [row for row in rows
                   if (row.get("session_id") or "legacy") == latest_id]
    latest_rows.sort(key=lambda row: int(row.get("request_index") or 0))
    latest_outcomes = Counter(str(row.get("outcome") or "unknown")
                              for row in latest_rows)
    latest_rets = Counter(str(row["ret"]) for row in latest_rows
                          if row.get("ret") is not None)
    latest_risk = next((row for row in latest_rows
                        if row.get("outcome") == "risk_control"), None)
    latest_risk_index = (int(latest_risk.get("request_index"))
                         if latest_risk and latest_risk.get("request_index") is not None
                         else None)
    latest_successes_before = sum(
        row.get("outcome") == "success"
        and (latest_risk_index is None
             or int(row.get("request_index") or 0) < latest_risk_index)
        for row in latest_rows
    )
    latest_times = []
    for row in latest_rows:
        try:
            latest_times.append(datetime.fromisoformat(
                row.get("started_at") or row["timestamp"]
            ))
        except (KeyError, TypeError, ValueError):
            continue
    latest_times.sort()
    latest_intervals = sorted(
        round((right - left).total_seconds(), 3)
        for left, right in zip(latest_times, latest_times[1:])
    )
    latest_interval_stats = {}
    if latest_intervals:
        def latest_percentile(fraction: float) -> float:
            return latest_intervals[
                min(len(latest_intervals) - 1,
                    max(0, int((len(latest_intervals) - 1) * fraction)))
            ]
        latest_interval_stats = {
            "min": latest_intervals[0], "p50": latest_percentile(0.5),
            "p95": latest_percentile(0.95), "max": latest_intervals[-1],
        }
    latest_auth_values = {row.get("authenticated") for row in latest_rows}
    latest_authenticated = (next(iter(latest_auth_values))
                            if len(latest_auth_values) == 1 else None)
    latest_session = {
        "session_id": latest_id,
        "authenticated": latest_authenticated,
        "total": len(latest_rows),
        "outcomes": dict(latest_outcomes),
        "ret_counts": dict(latest_rets),
        "first_risk_request_index": latest_risk_index,
        "successes_before_first_risk": latest_successes_before,
        "request_interval_seconds": latest_interval_stats,
        "max_in_flight": max((int(row.get("in_flight") or 0)
                              for row in latest_rows), default=0),
    }

    return {
        "total": len(rows),
        "outcomes": dict(outcomes),
        "ret_counts": dict(ret_counts),
        "first_risk_request_index": first_risk,
        "successes_before_first_risk": successes_before_first_risk,
        "recovery_seconds": recovery,
        "max_in_flight": max((int(row.get("in_flight") or 0) for row in rows),
                             default=0),
        "latency_ms": latency,
        "duration_seconds": round(duration, 3),
        "requests_per_minute": request_rate,
        "peak_requests_per_minute": peak_per_minute,
        "request_interval_seconds": interval_stats,
        "outcomes_by_in_flight": {key: dict(value)
                                  for key, value in by_in_flight.items()},
        "outcomes_by_authentication": {
            key: dict(value) for key, value in by_authentication.items()
        },
        "latest_session": latest_session,
    }
