"""Atomic WAL-mode SQLite storage backend using Python's built-in sqlite3."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from telbot.storage.base import ChatMemory, StorageBackend, UsageCounters

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_memory (
    scope_key TEXT PRIMARY KEY,
    messages_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id INTEGER PRIMARY KEY,
    facts_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_usage (
    user_id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    month TEXT NOT NULL,
    day_msgs INTEGER NOT NULL DEFAULT 0,
    day_searches INTEGER NOT NULL DEFAULT 0,
    day_images INTEGER NOT NULL DEFAULT 0,
    day_files INTEGER NOT NULL DEFAULT 0,
    month_msgs INTEGER NOT NULL DEFAULT 0,
    month_searches INTEGER NOT NULL DEFAULT 0,
    month_images INTEGER NOT NULL DEFAULT 0,
    month_files INTEGER NOT NULL DEFAULT 0,
    month_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS banned_users (
    target_id INTEGER PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT '',
    banned_by INTEGER NOT NULL DEFAULT 0,
    banned_at TEXT NOT NULL
);
"""


class SQLiteStorage(StorageBackend):
    """Thread-safe async wrapper around a WAL-mode SQLite database."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteStorage.init() must be called before use.")
        return self._conn

    @staticmethod
    def _now_iso(now: datetime | None = None) -> str:
        ts = now or datetime.now(timezone.utc)
        return ts.isoformat()

    def _init_sync(self) -> None:
        if self.db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        self._conn = conn

    async def init(self) -> None:
        async with self._lock:
            if self._conn is None:
                await asyncio.to_thread(self._init_sync)

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                conn = self._conn
                self._conn = None
                await asyncio.to_thread(conn.close)

    def _get_chat_memory_sync(self, scope_key: str) -> ChatMemory:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT messages_json, summary FROM chat_memory WHERE scope_key = ?",
            (scope_key,),
        ).fetchone()
        if not row:
            return ChatMemory()
        try:
            raw_msgs = json.loads(row["messages_json"])
        except json.JSONDecodeError:
            raw_msgs = []
        clean_msgs = [
            {"role": str(m["role"]), "content": str(m["content"])}
            for m in raw_msgs
            if isinstance(m, dict) and m.get("role") in {"user", "assistant"} and "content" in m
        ]
        return ChatMemory(messages=clean_msgs, summary=str(row["summary"] or ""))

    async def get_chat_memory(self, scope_key: str) -> ChatMemory:
        async with self._lock:
            return await asyncio.to_thread(self._get_chat_memory_sync, scope_key)

    def _save_chat_memory_sync(self, scope_key: str, memory: ChatMemory) -> None:
        conn = self._require_conn()
        with conn:
            conn.execute(
                """
                INSERT INTO chat_memory (scope_key, messages_json, summary, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    messages_json = excluded.messages_json,
                    summary = excluded.summary,
                    updated_at = excluded.updated_at
                """,
                (
                    scope_key,
                    json.dumps(memory.messages, ensure_ascii=False),
                    memory.summary,
                    self._now_iso(),
                ),
            )

    async def save_chat_memory(self, scope_key: str, memory: ChatMemory) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_chat_memory_sync, scope_key, memory)

    def _clear_chat_memory_sync(self, scope_key: str) -> None:
        conn = self._require_conn()
        with conn:
            conn.execute("DELETE FROM chat_memory WHERE scope_key = ?", (scope_key,))

    async def clear_chat_memory(self, scope_key: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._clear_chat_memory_sync, scope_key)

    def _get_user_profile_sync(self, user_id: int) -> list[str]:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT facts_json FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return []
        try:
            facts = json.loads(row["facts_json"])
        except json.JSONDecodeError:
            return []
        if not isinstance(facts, list):
            return []
        return [str(f).strip() for f in facts if str(f).strip()][:15]

    async def get_user_profile(self, user_id: int) -> list[str]:
        async with self._lock:
            return await asyncio.to_thread(self._get_user_profile_sync, user_id)

    def _save_user_profile_sync(self, user_id: int, facts: list[str]) -> None:
        conn = self._require_conn()
        clean = [f.strip()[:120] for f in facts if f and f.strip()][-15:]
        with conn:
            conn.execute(
                """
                INSERT INTO user_profiles (user_id, facts_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    facts_json = excluded.facts_json,
                    updated_at = excluded.updated_at
                """,
                (user_id, json.dumps(clean, ensure_ascii=False), self._now_iso()),
            )

    async def save_user_profile(self, user_id: int, facts: list[str]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_user_profile_sync, user_id, facts)

    def _clear_user_profile_sync(self, user_id: int) -> None:
        conn = self._require_conn()
        with conn:
            conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))

    async def clear_user_profile(self, user_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._clear_user_profile_sync, user_id)

    def _get_usage_sync(self, user_id: int, now: datetime | None = None) -> UsageCounters:
        conn = self._require_conn()
        row = conn.execute("SELECT * FROM user_usage WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return UsageCounters.empty(now)
        counters = UsageCounters(
            day=str(row["day"]),
            month=str(row["month"]),
            day_msgs=int(row["day_msgs"]),
            day_searches=int(row["day_searches"]),
            day_images=int(row["day_images"]),
            day_files=int(row["day_files"]),
            month_msgs=int(row["month_msgs"]),
            month_searches=int(row["month_searches"]),
            month_images=int(row["month_images"]),
            month_files=int(row["month_files"]),
            month_tokens=int(row["month_tokens"]),
        )
        return counters.roll_over(now)

    async def get_usage(self, user_id: int, *, now: datetime | None = None) -> UsageCounters:
        async with self._lock:
            return await asyncio.to_thread(self._get_usage_sync, user_id, now)

    def _increment_usage_sync(
        self,
        user_id: int,
        msgs: int,
        searches: int,
        images: int,
        files: int,
        tokens: int,
        now: datetime | None = None,
    ) -> UsageCounters:
        conn = self._require_conn()
        cur = self._get_usage_sync(user_id, now)
        updated = UsageCounters(
            day=cur.day,
            month=cur.month,
            day_msgs=cur.day_msgs + msgs,
            day_searches=cur.day_searches + searches,
            day_images=cur.day_images + images,
            day_files=cur.day_files + files,
            month_msgs=cur.month_msgs + msgs,
            month_searches=cur.month_searches + searches,
            month_images=cur.month_images + images,
            month_files=cur.month_files + files,
            month_tokens=cur.month_tokens + tokens,
        )
        with conn:
            conn.execute(
                """
                INSERT INTO user_usage (
                    user_id, day, month,
                    day_msgs, day_searches, day_images, day_files,
                    month_msgs, month_searches, month_images, month_files, month_tokens,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    day = excluded.day,
                    month = excluded.month,
                    day_msgs = excluded.day_msgs,
                    day_searches = excluded.day_searches,
                    day_images = excluded.day_images,
                    day_files = excluded.day_files,
                    month_msgs = excluded.month_msgs,
                    month_searches = excluded.month_searches,
                    month_images = excluded.month_images,
                    month_files = excluded.month_files,
                    month_tokens = excluded.month_tokens,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    updated.day,
                    updated.month,
                    updated.day_msgs,
                    updated.day_searches,
                    updated.day_images,
                    updated.day_files,
                    updated.month_msgs,
                    updated.month_searches,
                    updated.month_images,
                    updated.month_files,
                    updated.month_tokens,
                    self._now_iso(now),
                ),
            )
        return updated

    async def increment_usage(
        self,
        user_id: int,
        *,
        msgs: int = 0,
        searches: int = 0,
        images: int = 0,
        files: int = 0,
        tokens: int = 0,
        now: datetime | None = None,
    ) -> UsageCounters:
        async with self._lock:
            return await asyncio.to_thread(
                self._increment_usage_sync,
                user_id,
                msgs,
                searches,
                images,
                files,
                tokens,
                now,
            )

    def _get_global_model_sync(self) -> tuple[str, str] | None:
        conn = self._require_conn()
        row = conn.execute("SELECT value_json FROM bot_settings WHERE key = 'global_model'").fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["value_json"])
        except json.JSONDecodeError:
            return None
        if isinstance(data, list) and len(data) == 2 and all(isinstance(x, str) and x for x in data):
            return (data[0], data[1])
        return None

    async def get_global_model(self) -> tuple[str, str] | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_global_model_sync)

    def _set_global_model_sync(self, pref: tuple[str, str] | None) -> None:
        conn = self._require_conn()
        with conn:
            if pref is None:
                conn.execute("DELETE FROM bot_settings WHERE key = 'global_model'")
            else:
                conn.execute(
                    """
                    INSERT INTO bot_settings (key, value_json, updated_at)
                    VALUES ('global_model', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        updated_at = excluded.updated_at
                    """,
                    (json.dumps([pref[0], pref[1]]), self._now_iso()),
                )

    async def set_global_model(self, pref: tuple[str, str] | None) -> None:
        async with self._lock:
            await asyncio.to_thread(self._set_global_model_sync, pref)

    def _is_banned_sync(self, target_id: int) -> bool:
        conn = self._require_conn()
        row = conn.execute("SELECT 1 FROM banned_users WHERE target_id = ?", (target_id,)).fetchone()
        return row is not None

    async def is_banned(self, target_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._is_banned_sync, target_id)

    def _ban_target_sync(self, target_id: int, reason: str, banned_by: int) -> None:
        conn = self._require_conn()
        with conn:
            conn.execute(
                """
                INSERT INTO banned_users (target_id, reason, banned_by, banned_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    reason = excluded.reason,
                    banned_by = excluded.banned_by,
                    banned_at = excluded.banned_at
                """,
                (target_id, reason[:200], banned_by, self._now_iso()),
            )

    async def ban_target(self, target_id: int, reason: str, banned_by: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._ban_target_sync, target_id, reason, banned_by)

    def _unban_target_sync(self, target_id: int) -> bool:
        conn = self._require_conn()
        with conn:
            cur = conn.execute("DELETE FROM banned_users WHERE target_id = ?", (target_id,))
            return cur.rowcount > 0

    async def unban_target(self, target_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._unban_target_sync, target_id)

    def _list_bans_sync(self) -> list[tuple[int, str, int, str]]:
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT target_id, reason, banned_by, banned_at FROM banned_users ORDER BY banned_at DESC"
        ).fetchall()
        return [(int(r["target_id"]), str(r["reason"]), int(r["banned_by"]), str(r["banned_at"])) for r in rows]

    async def list_bans(self) -> list[tuple[int, str, int, str]]:
        async with self._lock:
            return await asyncio.to_thread(self._list_bans_sync)

    def _prune_inactive_sync(self, retention_days: int, now: datetime | None = None) -> int:
        if retention_days <= 0:
            return 0
        conn = self._require_conn()
        ts = now or datetime.now(timezone.utc)
        cutoff = (ts - timedelta(days=retention_days)).isoformat()
        with conn:
            c1 = conn.execute("DELETE FROM chat_memory WHERE updated_at < ?", (cutoff,)).rowcount
            c2 = conn.execute("DELETE FROM user_usage WHERE updated_at < ?", (cutoff,)).rowcount
            return int(c1) + int(c2)

    async def prune_inactive(self, retention_days: int, *, now: datetime | None = None) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._prune_inactive_sync, retention_days, now)

    def _get_stats_sync(self) -> dict[str, int]:
        conn = self._require_conn()
        return {
            "scopes": int(conn.execute("SELECT COUNT(*) FROM chat_memory").fetchone()[0]),
            "profiles": int(conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0]),
            "tracked_users": int(conn.execute("SELECT COUNT(*) FROM user_usage").fetchone()[0]),
            "banned_targets": int(conn.execute("SELECT COUNT(*) FROM banned_users").fetchone()[0]),
        }

    async def get_stats(self) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._get_stats_sync)
