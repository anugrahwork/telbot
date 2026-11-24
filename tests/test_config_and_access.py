"""Tests for typed Settings loading, fail-closed admin, access control, and group triggers."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telbot.config import ConfigError, Settings
from telbot.security.access import AccessController
from telbot.storage.sqlite_store import SQLiteStorage


def _base_env(**overrides: str) -> dict[str, str]:
    env = {
        "BOT_TOKEN": "123456:test-token",
        "AI_API_KEY": "sk-test-12345",
    }
    env.update(overrides)
    return env


class ConfigTests(unittest.TestCase):
    def test_requires_secrets_and_rejects_placeholders(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env({"BOT_TOKEN": "", "AI_API_KEY": "sk-ok"})
        with self.assertRaises(ConfigError):
            Settings.from_env({"BOT_TOKEN": "your_telegram_bot_token_here", "AI_API_KEY": "sk-ok"})
        with self.assertRaises(ConfigError):
            Settings.from_env({"BOT_TOKEN": "123:abc", "AI_API_KEY": "your_api_key_here"})

    def test_validates_port_range(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env(_base_env(PORT="abc"))
        with self.assertRaises(ConfigError):
            Settings.from_env(_base_env(PORT="70000"))

    def test_webhook_autodetection_for_render_and_railway(self) -> None:
        s_render = Settings.from_env(_base_env(RENDER_EXTERNAL_HOSTNAME="my-bot.onrender.com"))
        self.assertEqual(s_render.webhook_url, "https://my-bot.onrender.com/webhook")
        self.assertIn("my-bot.onrender.com", s_render.blocked_ssrf_hosts)

        s_rail = Settings.from_env(_base_env(RAILWAY_PUBLIC_DOMAIN="bot-prod.up.railway.app"))
        self.assertEqual(s_rail.webhook_url, "https://bot-prod.up.railway.app/webhook")
        self.assertIn("bot-prod.up.railway.app", s_rail.blocked_ssrf_hosts)

    def test_fail_closed_defaults(self) -> None:
        s = Settings.from_env(_base_env())
        self.assertEqual(s.admin_users, frozenset())
        self.assertEqual(s.group_trigger_mode, "mention_or_reply")
        self.assertTrue(s.allow_dms)
        self.assertTrue(s.allow_groups)
        self.assertEqual(s.ocr_api_key, "")

    def test_ignores_malformed_ids_with_double_minus(self) -> None:
        s = Settings.from_env(_base_env(ADMIN_USERS="123,--456,-,-789"))
        self.assertEqual(s.admin_users, frozenset({123, -789}))

    def test_parses_comma_separated_model_lists(self) -> None:
        s = Settings.from_env(
            _base_env(
                AI_MODEL=" model-1 , model-2, model-1 , ",
                FALLBACK_AI_MODEL="fb-1, fb-2 ",
            )
        )
        self.assertEqual(s.ai_model, "model-1")
        self.assertEqual(s.ai_models, ("model-1", "model-2"))
        self.assertEqual(s.fallback_ai_model, "fb-1")
        self.assertEqual(s.fallback_ai_models, ("fb-1", "fb-2"))

        s_empty = Settings.from_env(_base_env(FALLBACK_AI_MODEL=""))
        self.assertEqual(s_empty.fallback_ai_model, "")
        self.assertEqual(s_empty.fallback_ai_models, ())


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = SQLiteStorage(":memory:")
        await self.storage.init()

    async def asyncTearDown(self) -> None:
        await self.storage.close()

    async def test_fail_closed_admin_when_admin_users_empty(self) -> None:
        settings = Settings.from_env(_base_env(ADMIN_USERS=""))
        access = AccessController(settings, self.storage)
        self.assertFalse(access.is_admin(11111))
        self.assertFalse(access.is_admin(None))

    async def test_dm_and_group_policies(self) -> None:
        settings = Settings.from_env(_base_env(ADMIN_USERS="999", ALLOW_DMS="false"))
        access = AccessController(settings, self.storage)

        non_admin_dm = SimpleNamespace(
            effective_user=SimpleNamespace(id=101, is_bot=False),
            effective_chat=SimpleNamespace(id=101, type="private"),
            effective_message=SimpleNamespace(sender_chat=None),
        )
        ok, msg = await access.check_access(non_admin_dm, None)  # type: ignore[arg-type]
        self.assertFalse(ok)
        self.assertIn("disabled", msg.lower())

        admin_dm = SimpleNamespace(
            effective_user=SimpleNamespace(id=999, is_bot=False),
            effective_chat=SimpleNamespace(id=999, type="private"),
            effective_message=SimpleNamespace(sender_chat=None),
        )
        ok_adm, _ = await access.check_access(admin_dm, None)  # type: ignore[arg-type]
        self.assertTrue(ok_adm)

    async def test_unauthorized_group_auto_leaves(self) -> None:
        settings = Settings.from_env(_base_env(ALLOWED_GROUPS="-100111"))
        access = AccessController(settings, self.storage)

        bot_mock = SimpleNamespace(leave_chat=AsyncMock())
        ctx = SimpleNamespace(bot=bot_mock)
        upd = SimpleNamespace(
            effective_user=SimpleNamespace(id=50, is_bot=False),
            effective_chat=SimpleNamespace(id=-100222, type="supergroup"),
            effective_message=SimpleNamespace(sender_chat=None),
        )
        ok, _ = await access.check_access(upd, ctx)  # type: ignore[arg-type]
        self.assertFalse(ok)
        bot_mock.leave_chat.assert_awaited_once_with(-100222)

    async def test_group_trigger_mode_and_forum_topic_scoping(self) -> None:
        settings = Settings.from_env(_base_env(GROUP_TRIGGER_MODE="mention_or_reply"))
        access = AccessController(settings, self.storage)

        plain_msg = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-1005, type="supergroup", is_forum=True),
            effective_user=SimpleNamespace(id=42),
            effective_message=SimpleNamespace(text="hello everyone", caption=None, reply_to_message=None, message_thread_id=77),
        )
        self.assertFalse(access.should_respond_in_group(plain_msg, "my_comm_bot", 777))  # type: ignore[arg-type]
        self.assertEqual(access.memory_scope_key(plain_msg), "group:-1005:topic:77")  # type: ignore[arg-type]

        mention_msg = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-1005, type="supergroup", is_forum=False),
            effective_user=SimpleNamespace(id=42),
            effective_message=SimpleNamespace(text="@My_Comm_Bot summarize this", caption=None, reply_to_message=None),
        )
        self.assertTrue(access.should_respond_in_group(mention_msg, "my_comm_bot", 777))  # type: ignore[arg-type]

        reply_msg = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-1005, type="supergroup", is_forum=False),
            effective_user=SimpleNamespace(id=42),
            effective_message=SimpleNamespace(
                text="thanks",
                caption=None,
                reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=777, username="my_comm_bot")),
            ),
        )
        self.assertTrue(access.should_respond_in_group(reply_msg, "my_comm_bot", 777))  # type: ignore[arg-type]
