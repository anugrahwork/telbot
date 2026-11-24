"""Per-user & per-group sliding-window rate limits, concurrency locks, and daily quotas."""

from __future__ import annotations

import math
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from telbot.config import Settings
from telbot.storage.base import StorageBackend


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    message: str = ""
    retry_after: int = 0


class RateLimiter:
    """Protects public community deployments from spam bursts, concurrency abuse, and quota exhaustion."""

    def __init__(
        self,
        settings: Settings,
        storage: StorageBackend,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.clock = clock
        self._user_burst: dict[int, deque[float]] = {}
        self._user_rpm: dict[int, deque[float]] = {}
        self._group_rpm: dict[int, deque[float]] = {}
        self._active_users: set[int] = set()

    def _should_bypass(self, user_id: int) -> bool:
        return self.settings.admin_bypass_limits and user_id in self.settings.admin_users

    @staticmethod
    def _trim_window(dq: deque[float], now: float, window_seconds: float) -> None:
        cutoff = now - window_seconds
        while dq and dq[0] <= cutoff:
            dq.popleft()

    def _cleanup_stale_buckets(self, now: float) -> None:
        for store, window in (
            (self._user_burst, float(self.settings.rate_limit_burst_window)),
            (self._user_rpm, 60.0),
            (self._group_rpm, 60.0),
        ):
            if len(store) > 2000:
                stale = [
                    k
                    for k, dq in store.items()
                    if not dq or (now - dq[-1]) > window
                ]
                for k in stale:
                    del store[k]

    def check_rate_limit(self, user_id: int, chat_id: int | None = None, *, is_group: bool = False) -> RateLimitDecision:
        """Check burst, user RPM, and group RPM atomically; records hit only when allowed."""
        if self._should_bypass(user_id):
            return RateLimitDecision(True)

        now = self.clock()
        self._cleanup_stale_buckets(now)

        burst_dq = self._user_burst.setdefault(user_id, deque())
        self._trim_window(burst_dq, now, float(self.settings.rate_limit_burst_window))
        if self.settings.rate_limit_burst > 0 and len(burst_dq) >= self.settings.rate_limit_burst:
            retry = max(1, math.ceil(self.settings.rate_limit_burst_window - (now - burst_dq[0])))
            return RateLimitDecision(
                False,
                f"Slow down a little — please wait {retry}s before sending another request.",
                retry_after=retry,
            )

        rpm_dq = self._user_rpm.setdefault(user_id, deque())
        self._trim_window(rpm_dq, now, 60.0)
        if self.settings.rate_limit_per_minute > 0 and len(rpm_dq) >= self.settings.rate_limit_per_minute:
            retry = max(1, math.ceil(60.0 - (now - rpm_dq[0])))
            return RateLimitDecision(
                False,
                f"Rate limit reached ({self.settings.rate_limit_per_minute}/min). Try again in {retry}s.",
                retry_after=retry,
            )

        if is_group and chat_id is not None and self.settings.group_rate_limit_per_minute > 0:
            grp_dq = self._group_rpm.setdefault(chat_id, deque())
            self._trim_window(grp_dq, now, 60.0)
            if len(grp_dq) >= self.settings.group_rate_limit_per_minute:
                retry = max(1, math.ceil(60.0 - (now - grp_dq[0])))
                return RateLimitDecision(
                    False,
                    f"This group is hitting the shared pace limit. Please wait {retry}s.",
                    retry_after=retry,
                )
            grp_dq.append(now)

        burst_dq.append(now)
        rpm_dq.append(now)
        return RateLimitDecision(True)

    async def check_daily_quota(self, user_id: int, kind: str) -> RateLimitDecision:
        """Check persisted UTC daily quota for 'msgs', 'searches', 'images', or 'files'."""
        if self._should_bypass(user_id):
            return RateLimitDecision(True)

        limits = {
            "msgs": self.settings.daily_msg_limit,
            "searches": self.settings.daily_search_limit,
            "images": self.settings.daily_image_limit,
            "files": self.settings.daily_file_limit,
        }
        limit = limits.get(kind, 0)
        if limit <= 0:
            return RateLimitDecision(True)

        usage = await self.storage.get_usage(user_id)
        used_map = {
            "msgs": usage.day_msgs,
            "searches": usage.day_searches,
            "images": usage.day_images,
            "files": usage.day_files,
        }
        used = used_map.get(kind, 0)
        if used >= limit:
            label = {"msgs": "messages", "searches": "web searches", "images": "generated images", "files": "files"}[kind]
            return RateLimitDecision(
                False,
                f"Daily {label} quota reached ({used}/{limit}). Quotas reset at 00:00 UTC.",
            )
        return RateLimitDecision(True)

    def try_acquire_user_slot(self, user_id: int) -> bool:
        """Non-blocking concurrency lock: allow one active AI request per user at a time."""
        if user_id in self._active_users:
            return False
        self._active_users.add(user_id)
        return True

    def release_user_slot(self, user_id: int) -> None:
        self._active_users.discard(user_id)

    @asynccontextmanager
    async def user_slot(self, user_id: int) -> AsyncIterator[bool]:
        acquired = self.try_acquire_user_slot(user_id)
        try:
            yield acquired
        finally:
            if acquired:
                self.release_user_slot(user_id)
