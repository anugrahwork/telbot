"""Optional Supabase REST write-through cloud cache backed by local SQLiteStorage."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from telbot.storage.base import ChatMemory, StorageBackend, UsageCounters
from telbot.storage.sqlite_store import SQLiteStorage

logger = logging.getLogger(__name__)


class SupabaseStorage(StorageBackend):
    """Wraps SQLiteStorage with optional Supabase REST synchronization for chat memory."""

    def __init__(self, local: SQLiteStorage, supabase_url: str, supabase_key: str) -> None:
        self.local = local
        self.supabase_url = supabase_url.rstrip("/")
        self.supabase_key = supabase_key
        self._hydrated_scopes: set[str] = set()
        self._client: httpx.AsyncClient | None = None

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"apikey": self.supabase_key}
        if not self.supabase_key.startswith("sb_secret_"):
            headers["Authorization"] = f"Bearer {self.supabase_key}"
        if extra:
            headers.update(extra)
        return headers

    async def init(self) -> None:
        await self.local.init()
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await self.local.close()

    async def get_chat_memory(self, scope_key: str) -> ChatMemory:
        if scope_key not in self._hydrated_scopes and self._client is not None:
            self._hydrated_scopes.add(scope_key)
            try:
                resp = await self._client.get(
                    f"{self.supabase_url}/rest/v1/telbot_memory",
                    params={"scope_key": f"eq.{scope_key}", "select": "payload"},
                    headers=self._headers(),
                )
                resp.raise_for_status()
                rows = resp.json()
                if rows and isinstance(rows, list) and isinstance(rows[0].get("payload"), dict):
                    payload = rows[0]["payload"]
                    mem = ChatMemory(
                        messages=[
                            {"role": str(m["role"]), "content": str(m["content"])}
                            for m in (payload.get("messages") or [])
                            if isinstance(m, dict) and m.get("role") in {"user", "assistant"}
                        ],
                        summary=str(payload.get("summary") or ""),
                    )
                    await self.local.save_chat_memory(scope_key, mem)
                    return mem
            except Exception as exc:
                logger.warning("Supabase read fallback to SQLite for %s: %s", scope_key, exc)
        return await self.local.get_chat_memory(scope_key)

    async def save_chat_memory(self, scope_key: str, memory: ChatMemory) -> None:
        await self.local.save_chat_memory(scope_key, memory)
        self._hydrated_scopes.add(scope_key)
        if self._client is None:
            return
        try:
            resp = await self._client.post(
                f"{self.supabase_url}/rest/v1/telbot_memory",
                params={"on_conflict": "scope_key"},
                headers=self._headers(
                    {
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates",
                    }
                ),
                json={
                    "scope_key": scope_key,
                    "payload": {"messages": memory.messages, "summary": memory.summary},
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Supabase write fallback (kept in SQLite) for %s: %s", scope_key, exc)

    async def clear_chat_memory(self, scope_key: str) -> None:
        await self.local.clear_chat_memory(scope_key)
        self._hydrated_scopes.discard(scope_key)
        if self._client is None:
            return
        try:
            resp = await self._client.delete(
                f"{self.supabase_url}/rest/v1/telbot_memory",
                params={"scope_key": f"eq.{scope_key}"},
                headers=self._headers(),
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Supabase delete fallback for %s: %s", scope_key, exc)

    async def get_user_profile(self, user_id: int) -> list[str]:
        return await self.local.get_user_profile(user_id)

    async def save_user_profile(self, user_id: int, facts: list[str]) -> None:
        await self.local.save_user_profile(user_id, facts)

    async def clear_user_profile(self, user_id: int) -> None:
        await self.local.clear_user_profile(user_id)

    async def get_usage(self, user_id: int, *, now: datetime | None = None) -> UsageCounters:
        return await self.local.get_usage(user_id, now=now)

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
        return await self.local.increment_usage(
            user_id,
            msgs=msgs,
            searches=searches,
            images=images,
            files=files,
            tokens=tokens,
            now=now,
        )

    async def get_global_model(self) -> tuple[str, str] | None:
        return await self.local.get_global_model()

    async def set_global_model(self, pref: tuple[str, str] | None) -> None:
        await self.local.set_global_model(pref)

    async def is_banned(self, target_id: int) -> bool:
        return await self.local.is_banned(target_id)

    async def ban_target(self, target_id: int, reason: str, banned_by: int) -> None:
        await self.local.ban_target(target_id, reason, banned_by)

    async def unban_target(self, target_id: int) -> bool:
        return await self.local.unban_target(target_id)

    async def list_bans(self) -> list[tuple[int, str, int, str]]:
        return await self.local.list_bans()

    async def prune_inactive(self, retention_days: int, *, now: datetime | None = None) -> int:
        return await self.local.prune_inactive(retention_days, now=now)

    async def get_stats(self) -> dict[str, int]:
        return await self.local.get_stats()
