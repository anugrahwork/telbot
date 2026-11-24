"""Tests for WAL-mode SQLite storage, daily/monthly UTC rollovers, bans, and pruning."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from telbot.storage.base import ChatMemory
from telbot.storage.sqlite_store import SQLiteStorage


class SQLiteStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_memory_profiles_usage_rollover_bans_and_prune(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "bot.db")
            store = SQLiteStorage(db_path)
            await store.init()

            try:
                mem = ChatMemory(
                    messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
                    summary="User greeted the bot.",
                )
                await store.save_chat_memory("dm:10", mem)
                loaded = await store.get_chat_memory("dm:10")
                self.assertEqual(loaded.messages, mem.messages)
                self.assertEqual(loaded.summary, mem.summary)

                await store.save_user_profile(10, ["Prefers concise answers", "Lives in GMT+7"])
                self.assertEqual(await store.get_user_profile(10), ["Prefers concise answers", "Lives in GMT+7"])

                day1 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
                day2 = datetime(2026, 11, 21, 1, 0, tzinfo=timezone.utc)
                next_month = datetime(2026, 12, 1, 0, 5, tzinfo=timezone.utc)

                u1 = await store.increment_usage(10, msgs=3, searches=2, tokens=500, now=day1)
                self.assertEqual(u1.day_msgs, 3)
                self.assertEqual(u1.month_msgs, 3)

                u2 = await store.get_usage(10, now=day2)
                self.assertEqual(u2.day_msgs, 0)
                self.assertEqual(u2.month_msgs, 3)

                u3 = await store.get_usage(10, now=next_month)
                self.assertEqual(u3.month_msgs, 0)
                self.assertEqual(u3.month_tokens, 0)

                await store.set_global_model(("primary", "gpt-4o-mini"))
                self.assertEqual(await store.get_global_model(), ("primary", "gpt-4o-mini"))

                await store.ban_target(555, "spamming", 999)
                self.assertTrue(await store.is_banned(555))
                self.assertEqual(len(await store.list_bans()), 1)
                self.assertTrue(await store.unban_target(555))
                self.assertFalse(await store.is_banned(555))

                # Prune everything with a future timestamp 31 days ahead
                future = datetime.now(timezone.utc) + timedelta(days=31)
                pruned = await store.prune_inactive(30, now=future)
                self.assertGreaterEqual(pruned, 1)
                self.assertEqual((await store.get_chat_memory("dm:10")).messages, [])
            finally:
                await store.close()

            # Re-open to verify durability across restarts
            store2 = SQLiteStorage(db_path)
            await store2.init()
            try:
                self.assertEqual(await store2.get_global_model(), ("primary", "gpt-4o-mini"))
                self.assertEqual(await store2.get_user_profile(10), ["Prefers concise answers", "Lives in GMT+7"])
            finally:
                await store2.close()
