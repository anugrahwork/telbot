"""Tests for burst, RPM, group throttle, concurrency slots, and daily quotas."""

from __future__ import annotations

import unittest

from telbot.config import Settings
from telbot.security.rate_limit import RateLimiter
from telbot.storage.sqlite_store import SQLiteStorage


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = SQLiteStorage(":memory:")
        await self.storage.init()

    async def asyncTearDown(self) -> None:
        await self.storage.close()

    async def test_burst_and_rpm_sliding_windows_do_not_penalize_rejected_requests(self) -> None:
        clock = FakeClock()
        settings = Settings.from_env(
            {
                "BOT_TOKEN": "123:abc",
                "AI_API_KEY": "sk-test",
                "RATE_LIMIT_BURST": "2",
                "RATE_LIMIT_BURST_WINDOW": "10",
                "RATE_LIMIT_PER_MINUTE": "3",
                "ADMIN_USERS": "999",
            }
        )
        rl = RateLimiter(settings, self.storage, clock=clock)

        self.assertTrue(rl.check_rate_limit(10).allowed)
        self.assertTrue(rl.check_rate_limit(10).allowed)
        denied = rl.check_rate_limit(10)
        self.assertFalse(denied.allowed)
        self.assertGreaterEqual(denied.retry_after, 1)

        # Rejected spam attempts shouldn't extend the burst window
        clock.advance(5)
        self.assertFalse(rl.check_rate_limit(10).allowed)
        clock.advance(6)  # now +11s from initial hits -> burst window cleared
        self.assertTrue(rl.check_rate_limit(10).allowed)
        # Total RPM hits for user 10 is now 3 -> 4th within 60s hits RPM limit
        denied_rpm = rl.check_rate_limit(10)
        self.assertFalse(denied_rpm.allowed)
        self.assertIn("rate limit", denied_rpm.message.lower())

        # Admin bypasses limits
        for _ in range(10):
            self.assertTrue(rl.check_rate_limit(999).allowed)

    async def test_daily_quota_and_concurrency_slot(self) -> None:
        settings = Settings.from_env(
            {
                "BOT_TOKEN": "123:abc",
                "AI_API_KEY": "sk-test",
                "DAILY_MSG_LIMIT": "2",
                "DAILY_SEARCH_LIMIT": "1",
                "ADMIN_USERS": "999",
            }
        )
        rl = RateLimiter(settings, self.storage)

        self.assertTrue(rl.try_acquire_user_slot(55))
        self.assertFalse(rl.try_acquire_user_slot(55))
        rl.release_user_slot(55)
        self.assertTrue(rl.try_acquire_user_slot(55))
        rl.release_user_slot(55)

        self.assertTrue((await rl.check_daily_quota(55, "msgs")).allowed)
        await self.storage.increment_usage(55, msgs=2, searches=1)
        q_msg = await rl.check_daily_quota(55, "msgs")
        self.assertFalse(q_msg.allowed)
        self.assertIn("00:00 utc", q_msg.message.lower())
        q_search = await rl.check_daily_quota(55, "searches")
        self.assertFalse(q_search.allowed)
        self.assertTrue((await rl.check_daily_quota(999, "msgs")).allowed)
