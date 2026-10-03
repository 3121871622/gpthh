"""线程安全、可持久化的绘画会话统计。"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

MAX_RECORDS = 50
STATS_FILENAME = "plugin_stats.json"
STATS_SCHEMA_VERSION = 2
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class PluginStats:
    """统计一次用户会话，兼容旧的 ``totals`` 与 ``recent`` 字段。"""

    def __init__(self, data_dir: str = "") -> None:
        self._lock = threading.RLock()
        directory = _text(data_dir)
        self._path = Path(directory) / STATS_FILENAME if directory else None
        self.success = self.failed = self.cancelled = self.partial = 0
        self.images = self.retries = self.sessions_started = 0
        self.total_seconds = 0.0
        self.last_error = self.last_time = ""
        self.started_at = time.strftime(_TIME_FORMAT)
        self._recent: list[dict[str, Any]] = []
        self._active_sessions: set[str] = set()
        self._closed_sessions: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            with self._lock:
                if not self._path.is_file():
                    return
                with self._path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if not isinstance(payload, dict):
                    return
                totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else payload
                for name in ("success", "failed", "cancelled", "partial", "images", "retries", "sessions_started"):
                    setattr(self, name, max(0, _int(totals.get(name))))
                seconds = totals.get("total_seconds")
                if seconds is None:
                    seconds = _float(totals.get("avg_seconds")) * max(0, self.success + self.failed + self.cancelled)
                self.total_seconds = max(0.0, _float(seconds))
                self.last_error = _text(totals.get("last_error"))[:200]
                self.last_time = _text(totals.get("last_time"))
                self.started_at = _text(payload.get("started_at")) or self.started_at
                recent = payload.get("recent")
                if isinstance(recent, list):
                    self._recent = [dict(item) for item in recent[:MAX_RECORDS] if isinstance(item, dict)]
                closed = payload.get("closed_sessions")
                if isinstance(closed, list):
                    self._closed_sessions = {_text(item) for item in closed if _text(item)}
        except Exception:
            pass

    def _persist_locked(self) -> None:
        if self._path is None:
            return
        temporary = ""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = self._snapshot_locked()
            payload["schema_version"] = STATS_SCHEMA_VERSION
            payload["totals"]["total_seconds"] = round(self.total_seconds, 4)
            payload["closed_sessions"] = list(self._closed_sessions)[-MAX_RECORDS * 5:]
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(self._path.parent), prefix=self._path.name + ".", suffix=".tmp", delete=False) as handle:
                temporary = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, str(self._path))
        except Exception:
            if temporary:
                try:
                    os.unlink(temporary)
                except Exception:
                    pass

    def record_start(self, *, session_id: str, **kwargs: Any) -> None:
        try:
            identifier = _text(session_id)
            if not identifier:
                return
            with self._lock:
                if identifier in self._active_sessions or identifier in self._closed_sessions:
                    return
                self._active_sessions.add(identifier)
                self.sessions_started += 1
                self._persist_locked()
        except Exception:
            pass

    def record_success(self, *, kind: str = "", seconds: float = 0.0, model: str = "", protocol: str = "", supplier: str = "", images: int = 0, session_id: str = "", requested: int = 1, failed: int = 0, retries: int = 0) -> None:
        try:
            with self._lock:
                if not self._finish_once_locked(session_id):
                    return
                duration, count = max(0.0, _float(seconds)), max(0, _int(images))
                requested_count, failed_count = max(0, _int(requested)), max(0, _int(failed))
                retry_count = max(0, _int(retries))
                self.success += 1
                self.images += count
                self.total_seconds += duration
                self.retries += retry_count
                stamp = time.strftime(_TIME_FORMAT)
                self.last_time = stamp
                self._push_locked(self._entry(stamp, "success", kind, duration, model, protocol, supplier, count, requested_count, max(0, requested_count - failed_count), failed_count, retry_count, "", session_id))
                self._persist_locked()
        except Exception:
            pass

    def record_failure(self, *, kind: str = "", seconds: float = 0.0, model: str = "", protocol: str = "", supplier: str = "", error: str = "", images: int = 0, session_id: str = "", requested: int = 1, succeeded: int = 0, retries: int = 0) -> None:
        try:
            with self._lock:
                if not self._finish_once_locked(session_id):
                    return
                duration, succeeded_count = max(0.0, _float(seconds)), max(0, _int(succeeded))
                requested_count, retry_count = max(0, _int(requested)), max(0, _int(retries))
                message = _text(error)[:200]
                self.failed += 1
                self.partial += 1 if succeeded_count else 0
                self.images += max(0, _int(images))
                self.total_seconds += duration
                self.retries += retry_count
                self.last_error = message
                stamp = time.strftime(_TIME_FORMAT)
                self.last_time = stamp
                status = "partial" if succeeded_count else "failed"
                self._push_locked(self._entry(stamp, status, kind, duration, model, protocol, supplier, images, requested_count, succeeded_count, max(0, requested_count - succeeded_count), retry_count, message, session_id))
                self._persist_locked()
        except Exception:
            pass

    def record_cancel(self, *, session_id: str, kind: str = "", seconds: float = 0.0, model: str = "", protocol: str = "", supplier: str = "", requested: int = 1, succeeded: int = 0, retries: int = 0, error: str = "") -> None:
        try:
            with self._lock:
                if not self._finish_once_locked(session_id):
                    return
                duration, retry_count = max(0.0, _float(seconds)), max(0, _int(retries))
                self.cancelled += 1
                self.total_seconds += duration
                self.retries += retry_count
                stamp = time.strftime(_TIME_FORMAT)
                self.last_time = stamp
                self._push_locked(self._entry(stamp, "cancelled", kind, duration, model, protocol, supplier, 0, requested, succeeded, 0, retry_count, _text(error)[:200] or "会话已取消", session_id))
                self._persist_locked()
        except Exception:
            pass

    def has_session(self, session_id: str) -> bool:
        try:
            with self._lock:
                return _text(session_id) in self._active_sessions
        except Exception:
            return False

    def _finish_once_locked(self, session_id: str) -> bool:
        identifier = _text(session_id)
        if not identifier:
            return True
        if identifier in self._closed_sessions:
            return False
        self._active_sessions.discard(identifier)
        self._closed_sessions.add(identifier)
        if len(self._closed_sessions) > MAX_RECORDS * 10:
            self._closed_sessions = set(list(self._closed_sessions)[-MAX_RECORDS * 5:])
        return True

    def _entry(self, stamp: str, status: str, kind: str, seconds: float, model: str, protocol: str, supplier: str, images: int, requested: int, succeeded: int, failed: int, retries: int, error: str, session_id: str) -> dict[str, Any]:
        labels = {"success": "成功", "failed": "失败", "partial": "部分成功", "cancelled": "已取消"}
        return {"time": stamp, "session_id": _text(session_id), "status": status, "status_label": labels.get(status, status), "ok": status == "success", "kind": _text(kind) or "draw", "seconds": round(seconds, 2), "model": _text(model), "protocol": _text(protocol), "supplier": _text(supplier), "images": max(0, _int(images)), "requested": max(0, _int(requested)), "succeeded": max(0, _int(succeeded)), "failed": max(0, _int(failed)), "retries": max(0, _int(retries)), "error": _text(error)[:200]}

    def _push_locked(self, entry: dict[str, Any]) -> None:
        self._recent.insert(0, entry)
        del self._recent[MAX_RECORDS:]

    def reset(self) -> None:
        try:
            with self._lock:
                self.success = self.failed = self.cancelled = self.partial = 0
                self.images = self.retries = self.sessions_started = 0
                self.total_seconds = 0.0
                self.last_error = self.last_time = ""
                self._recent = []
                self._active_sessions = set()
                self._closed_sessions = set()
                self._persist_locked()
        except Exception:
            pass

    def _snapshot_locked(self) -> dict[str, Any]:
        total = self.success + self.failed + self.cancelled
        statuses = {"success": self.success, "failed": max(0, self.failed - self.partial), "partial": self.partial, "cancelled": self.cancelled}
        return {"totals": {"success": self.success, "failed": self.failed, "cancelled": self.cancelled, "partial": self.partial, "images": self.images, "total": total, "completed": self.success + self.failed, "sessions_started": self.sessions_started, "in_progress": len(self._active_sessions), "retries": self.retries, "avg_seconds": round(self.total_seconds / total, 2) if total else 0.0, "last_error": self.last_error, "last_time": self.last_time}, "status_counts": statuses, "by_status": dict(statuses), "session_totals": {**statuses, "total": total, "success_rate": round(self.success * 100 / total, 2) if total else 0.0}, "recent": copy.deepcopy(self._recent), "recent_sessions": copy.deepcopy(self._recent), "started_at": self.started_at}

    def snapshot(self) -> dict[str, Any]:
        try:
            with self._lock:
                return self._snapshot_locked()
        except Exception:
            return _empty_snapshot()

    def summary_text(self) -> str:
        try:
            return "成功 {success} 次 · 失败 {failed} 次 · 取消 {cancelled} 次 · 出图 {images} 张 · 平均耗时 {avg_seconds} 秒".format(**self.snapshot()["totals"])
        except Exception:
            return "暂无可用的统计数据"


def _empty_snapshot() -> dict[str, Any]:
    statuses = {"success": 0, "failed": 0, "partial": 0, "cancelled": 0}
    return {"totals": {"success": 0, "failed": 0, "cancelled": 0, "partial": 0, "images": 0, "total": 0, "completed": 0, "sessions_started": 0, "in_progress": 0, "retries": 0, "avg_seconds": 0.0, "last_error": "", "last_time": ""}, "status_counts": statuses.copy(), "by_status": statuses.copy(), "session_totals": {**statuses, "total": 0, "success_rate": 0.0}, "recent": [], "recent_sessions": [], "started_at": ""}


def _text(value: Any) -> str:
    try:
        return "" if value is None else str(value).strip()
    except Exception:
        return ""


def _int(value: Any) -> int:
    try:
        return 0 if isinstance(value, bool) else int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["MAX_RECORDS", "STATS_FILENAME", "STATS_SCHEMA_VERSION", "PluginStats"]
