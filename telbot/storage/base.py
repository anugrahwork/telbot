"""Abstract storage interface and typed data models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ChatMemory:
    """Recent messages and rolling summary for a DM user or group/topic scope."""

    messages: list[dict[str, str]] = field(default_factory=list)
    summary: str = ""


@dataclass
class UsageCounters:
    """Tracked daily and monthly per-user usage counters (UTC)."""

    day: str
    month: str
    day_msgs: int = 0
    day_searches: int = 0
    day_images: int = 0
    day_files: int = 0
    month_msgs: int = 0
    month_searches: int = 0
    month_images: int = 0
    month_files: int = 0
    month_tokens: int = 0

    @classmethod
    def empty(cls, now: datetime | None = None) -> "UsageCounters":
        ts = now or datetime.now(timezone.utc)
        return cls(day=ts.strftime("%Y-%m-%d"), month=ts.strftime("%Y-%m"))

    def roll_over(self, now: datetime | None = None) -> "UsageCounters":
        """Return counters rolled over to the current UTC day/month if needed."""
        ts = now or datetime.now(timezone.utc)
        today = ts.strftime("%Y-%m-%d")
        month = ts.strftime("%Y-%m")
        day_changed = self.day != today
        month_changed = self.month != month
        return UsageCounters(
            day=today,
            month=month,
            day_msgs=0 if day_changed else self.day_msgs,
            day_searches=0 if day_changed else self.day_searches,
            day_images=0 if day_changed else self.day_images,
            day_files=0 if day_changed else self.day_files,
            month_msgs=0 if month_changed else self.month_msgs,
            month_searches=0 if month_changed else self.month_searches,
            month_images=0 if month_changed else self.month_images,
            month_files=0 if month_changed else self.month_files,
            month_tokens=0 if month_changed else self.month_tokens,
        )


class StorageBackend(ABC):
    """Protocol for bot persistence backends."""

    @abstractmethod
    async def init(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def get_chat_memory(self, scope_key: str) -> ChatMemory: ...

    @abstractmethod
    async def save_chat_memory(self, scope_key: str, memory: ChatMemory) -> None: ...

    @abstractmethod
    async def clear_chat_memory(self, scope_key: str) -> None: ...

    @abstractmethod
    async def get_user_profile(self, user_id: int) -> list[str]: ...

    @abstractmethod
    async def save_user_profile(self, user_id: int, facts: list[str]) -> None: ...

    @abstractmethod
    async def clear_user_profile(self, user_id: int) -> None: ...

    @abstractmethod
    async def get_usage(self, user_id: int, *, now: datetime | None = None) -> UsageCounters: ...

    @abstractmethod
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
    ) -> UsageCounters: ...

    @abstractmethod
    async def get_global_model(self) -> tuple[str, str] | None: ...

    @abstractmethod
    async def set_global_model(self, pref: tuple[str, str] | None) -> None: ...

    @abstractmethod
    async def is_banned(self, target_id: int) -> bool: ...

    @abstractmethod
    async def ban_target(self, target_id: int, reason: str, banned_by: int) -> None: ...

    @abstractmethod
    async def unban_target(self, target_id: int) -> bool: ...

    @abstractmethod
    async def list_bans(self) -> list[tuple[int, str, int, str]]: ...

    @abstractmethod
    async def prune_inactive(self, retention_days: int, *, now: datetime | None = None) -> int: ...

    @abstractmethod
    async def get_stats(self) -> dict[str, int]: ...
