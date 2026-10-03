# -*- coding: utf-8 -*-
"""AstrBot \u63d2\u4ef6\u300cgpt-image-2.5\u7ed8\u753b\u300d\u2014\u2014\u73a9\u6cd5\u7edf\u8ba1\uff08\u62bd\u5361 / \u699c\u5355\uff09\u3002

\u5168\u90e8\u65f6\u95f4\u76f8\u5173\u7684\u7edf\u8ba1\u90fd\u4ee5\u300c\u81ea\u7136\u65e5\u300d\u4e3a\u7c92\u5ea6\uff0c\u907f\u514d\u8de8\u5929\u7d2f\u52a0\u5bfc\u81f4\u699c\u5355\u5931\u771f\u3002
"""
from __future__ import annotations

import io
import json
import os
import time
from typing import Any, Optional

#: \u6570\u636e\u6587\u4ef6\u540d\uff08\u4f5c\u4e1a\u76ee\u5f55\u7531\u4e3b\u63a7\u5236\u4f20\u5165\uff09
DATA_FILENAME = "fun_stats.json"

#: \u699c\u5355\u4fdd\u7559\u4eba\u6570
RANK_LIMIT = 10

#: \u6bcf\u4eba\u6bcf\u5929\u62bd\u5361\u6b21\u6570\u4e0a\u9650
GACHA_PER_DAY = 1


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001
        return ""


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return 0


class FunStats:
    """\u62bd\u5361\u4e0e\u699c\u5355\u7edf\u8ba1\uff08\u8fdb\u7a0b\u5185 + \u53ef\u9009\u843d\u76d8\uff09\u3002"""

    def __init__(self, data_dir: str = "") -> None:
        self._dir = _text(data_dir)
        self._loaded = False
        #: {"2026-09-12": {"counts": {user_id: n}, "replies": {user_id: n}, "gacha": {user_id: {"date":..., "style":...}}}}
        self._data: dict = {}

    # ------------------------------------------------------------------ \u5b58\u50a8
    def _path(self) -> str:
        if not self._dir:
            return ""
        return os.path.join(self._dir, DATA_FILENAME)

    def load(self) -> None:
        """\u4ece\u78c1\u76d8\u8bfb\u53d6\uff1b\u4efb\u4f55\u5f02\u5e38\u90fd\u9759\u9ed8\u53d6\u6d88\uff0c\u4e0d\u5f71\u54cd\u63d2\u4ef6\u542f\u52a8\u3002"""
        if self._loaded:
            return
        self._loaded = True
        path = self._path()
        if not path or not os.path.isfile(path):
            return
        try:
            with io.open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                self._data = payload
        except Exception:  # noqa: BLE001
            self._data = {}

    def save(self) -> bool:
        """\u539f\u5b50\u5199\u5165\u78c1\u76d8\uff1b\u5931\u8d25\u8fd4\u56de False\uff08\u4e0d\u629b\u5f02\u5e38\uff09\u3002"""
        path = self._path()
        if not path:
            return False
        try:
            directory = os.path.dirname(path)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            tmp = path + ".tmp"
            with io.open(tmp, "w", encoding="utf-8", newline="") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _prune(self, keep_days: int = 30) -> None:
        """\u53ea\u4fdd\u7559\u6700\u8fd1\u82e5\u5e72\u5929\uff0c\u907f\u514d\u6570\u636e\u65e0\u9650\u589e\u957f\u3002"""
        if len(self._data) <= keep_days:
            return
        for day in sorted(self._data.keys())[:-keep_days]:
            self._data.pop(day, None)

    def _bucket(self, day: str = "") -> dict:
        key = day or _today()
        bucket = self._data.get(key)
        if not isinstance(bucket, dict):
            bucket = {"counts": {}, "replies": {}, "gacha": {}}
            self._data[key] = bucket
        for field in ("counts", "replies", "gacha"):
            if not isinstance(bucket.get(field), dict):
                bucket[field] = {}
        return bucket

    # ------------------------------------------------------------------ \u8bb0\u5f55
    def record_draw(self, user_id: str, user_name: str = "", count: int = 1) -> None:
        """\u8bb0\u5f55\u4e00\u6b21\u51fa\u56fe\uff08\u6309\u7528\u6237\u7d2f\u52a0\uff09\u3002"""
        try:
            uid = _text(user_id)
            if not uid:
                return
            bucket = self._bucket()
            entry = bucket["counts"].get(uid)
            if not isinstance(entry, dict):
                entry = {"count": 0, "name": ""}
            entry["count"] = _as_int(entry.get("count")) + max(1, _as_int(count) or 1)
            if _text(user_name):
                entry["name"] = _text(user_name)
            bucket["counts"][uid] = entry
            self._prune()
        except Exception:  # noqa: BLE001
            pass

    def record_reply(self, user_id: str, user_name: str = "") -> None:
        """\u8bb0\u5f55\u4e00\u6b21\u88ab\u56de\u590d\uff08\u88ab\u5f15\u7528\uff09\uff1b\u7528\u4e8e\u300c\u88ab\u56de\u590d\u699c\u300d\u3002"""
        try:
            uid = _text(user_id)
            if not uid:
                return
            bucket = self._bucket()
            entry = bucket["replies"].get(uid)
            if not isinstance(entry, dict):
                entry = {"count": 0, "name": ""}
            entry["count"] = _as_int(entry.get("count")) + 1
            if _text(user_name):
                entry["name"] = _text(user_name)
            bucket["replies"][uid] = entry
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ \u62bd\u5361
    def gacha_state(self, user_id: str) -> dict:
        """\u8fd4\u56de\u4eca\u65e5\u62bd\u5361\u72b6\u6001\uff1a``{"used": bool, "style": str, "date": str}``\u3002"""
        result = {"used": False, "style": "", "date": _today()}
        try:
            uid = _text(user_id)
            if not uid:
                return result
            bucket = self._bucket()
            entry = bucket["gacha"].get(uid)
            if isinstance(entry, dict) and _text(entry.get("date")) == _today():
                result["used"] = True
                result["style"] = _text(entry.get("style"))
        except Exception:  # noqa: BLE001
            pass
        return result

    def gacha_used_today(self, user_id: str) -> bool:
        return bool(self.gacha_state(user_id).get("used"))

    def mark_gacha(self, user_id: str, style: str = "") -> bool:
        """\u8bb0\u5f55\u4eca\u65e5\u5df2\u62bd\u5361\uff1b\u5df2\u62bd\u8fc7\u5219\u8fd4\u56de False\u3002"""
        try:
            uid = _text(user_id)
            if not uid:
                return False
            if self.gacha_used_today(uid):
                return False
            bucket = self._bucket()
            bucket["gacha"][uid] = {"date": _today(), "style": _text(style)}
            self._prune()
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ \u699c\u5355
    def ranking(self, kind: str = "draw", limit: int = RANK_LIMIT) -> list:
        """\u8fd4\u56de\u6307\u5b9a\u7ef4\u5ea6\u7684\u699c\u5355\uff08\u7d2f\u8ba1\uff09\u3002

        ``kind``\uff1a``draw`` = \u51fa\u56fe\u6570\uff1b``reply`` = \u88ab\u56de\u590d\u6570\u3002
        \u8fd4\u56de ``[{"user_id","name","count"}]``\uff0c\u6309\u8ba1\u6570\u964d\u5e8f\uff0c\u540c\u5206\u6309\u7528\u6237 ID \u7a33\u5b9a\u6392\u5e8f\u3002
        """
        field = "replies" if _text(kind).lower() == "reply" else "counts"
        merged: dict = {}
        for bucket in self._data.values():
            if not isinstance(bucket, dict):
                continue
            entries = bucket.get(field)
            if not isinstance(entries, dict):
                continue
            for uid, entry in entries.items():
                if not isinstance(entry, dict):
                    continue
                current = merged.get(uid)
                if current is None:
                    current = {"user_id": uid, "name": "", "count": 0}
                    merged[uid] = current
                current["count"] += _as_int(entry.get("count"))
                if _text(entry.get("name")):
                    current["name"] = _text(entry.get("name"))
        rows = [row for row in merged.values() if row["count"] > 0]
        rows.sort(key=lambda row: (-row["count"], row["user_id"]))
        return rows[: max(1, _as_int(limit) or RANK_LIMIT)]

    def today_ranking(self, kind: str = "draw", limit: int = RANK_LIMIT) -> list:
        """\u4eca\u65e5\u699c\u5355\u3002"""
        field = "replies" if _text(kind).lower() == "reply" else "counts"
        bucket = self._bucket()
        entries = bucket.get(field) or {}
        rows = []
        for uid, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            count = _as_int(entry.get("count"))
            if count <= 0:
                continue
            rows.append({"user_id": uid, "name": _text(entry.get("name")), "count": count})
        rows.sort(key=lambda row: (-row["count"], row["user_id"]))
        return rows[: max(1, _as_int(limit) or RANK_LIMIT)]

    def total_draws(self) -> int:
        total = 0
        for bucket in self._data.values():
            if not isinstance(bucket, dict):
                continue
            for entry in (bucket.get("counts") or {}).values():
                if isinstance(entry, dict):
                    total += _as_int(entry.get("count"))
        return total

    def reset(self) -> None:
        self._data = {}
        self.save()


__all__ = ["DATA_FILENAME", "GACHA_PER_DAY", "RANK_LIMIT", "FunStats"]