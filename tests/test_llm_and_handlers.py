"""End-to-end offline tests for LLM router failover, handlers, search loop safety, and admin checks."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telbot.config import Settings
from telbot.handlers import AppServices, SERVICES_KEY
from telbot.handlers.commands import ask_command, ban_command, reset_command, usage_command
from telbot.handlers.messages import error_handler, extract_first_url, handle_message, send_chat_action_safe
from telbot.security.access import AccessController
from telbot.security.rate_limit import RateLimiter
from telbot.services.llm import LLMRouter
from telbot.services.media import MediaService
from telbot.services.search import SearchService
from telbot.services.vision import VisionService
from telbot.storage.sqlite_store import SQLiteStorage


def _make_completion(text: str, total_tokens: int = 42):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(total_tokens=total_tokens),
    )


class LLMRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_failure_falls_back_to_secondary_provider(self) -> None:
        settings = Settings.from_env(
            {
                "BOT_TOKEN": "123:abc",
                "AI_API_KEY": "sk-primary",
                "AI_BASE_URL": "https://api.primary.example/v1",
                "AI_MODEL": "model-a",
                "FALLBACK_AI_API_KEY": "sk-fallback",
                "FALLBACK_AI_BASE_URL": "https://api.fallback.example/v1",
                "FALLBACK_AI_MODEL": "model-b",
            }
        )
        router = LLMRouter(settings, retry_delay_seconds=0.0)
        try:
            router.primary_client.chat.completions.create = AsyncMock(  # type: ignore[method-assign]
                side_effect=RuntimeError("429 rate limit exceeded")
            )
            assert router.fallback_client is not None
            router.fallback_client.chat.completions.create = AsyncMock(  # type: ignore[method-assign]
                return_value=_make_completion("Fallback worked!", total_tokens=100)
            )
            text, tokens = await router.generate([{"role": "user", "content": "hi"}])
            self.assertEqual(text, "Fallback worked!")
            self.assertEqual(tokens, 100)
            self.assertIn(("primary", "model-a"), router.model_cooldown_until)
        finally:
            await router.aclose()

    async def test_comma_separated_models_fail_over_individually(self) -> None:
        settings = Settings.from_env(
            {
                "BOT_TOKEN": "123:tok",
                "AI_API_KEY": "sk-primary",
                "AI_BASE_URL": "https://primary.example/v1",
                "AI_MODEL": "p1, p2",
                "FALLBACK_AI_API_KEY": "sk-fallback",
                "FALLBACK_AI_BASE_URL": "https://fallback.example/v1",
                "FALLBACK_AI_MODEL": "f1, f2",
            }
        )
        router = LLMRouter(settings, retry_delay_seconds=0.0)
        try:
            router.primary_client.chat.completions.create = AsyncMock(  # type: ignore[method-assign]
                side_effect=[
                    RuntimeError("Error code: 429"),
                    RuntimeError("Error code: 429"),
                ]
            )
            assert router.fallback_client is not None
            router.fallback_client.chat.completions.create = AsyncMock(  # type: ignore[method-assign]
                side_effect=[
                    RuntimeError("Error code: 429"),
                    _make_completion("Second fallback model worked!", total_tokens=42),
                ]
            )
            text, tokens = await router.generate([{"role": "user", "content": "hi"}])
            self.assertEqual(text, "Second fallback model worked!")
            self.assertEqual(tokens, 42)
            primary_models = [c.kwargs["model"] for c in router.primary_client.chat.completions.create.await_args_list]
            fallback_models = [c.kwargs["model"] for c in router.fallback_client.chat.completions.create.await_args_list]
            self.assertEqual(primary_models, ["p1", "p2"])
            self.assertEqual(fallback_models, ["f1", "f2"])
            self.assertIn(("fallback", "f1"), router.model_cooldown_until)
            self.assertNotIn(("fallback", "f2"), router.model_cooldown_until)
        finally:
            await router.aclose()

    async def test_transient_rate_limit_retries_once(self) -> None:
        settings = Settings.from_env(
            {
                "BOT_TOKEN": "123:tok",
                "AI_API_KEY": "sk-primary",
                "AI_BASE_URL": "https://primary.example/v1",
                "AI_MODEL": "primary-a",
            }
        )
        router = LLMRouter(settings, retry_delay_seconds=0.0)
        try:
            router.primary_client.chat.completions.create = AsyncMock(  # type: ignore[method-assign]
                side_effect=[
                    RuntimeError("Error code: 429"),
                    _make_completion("Recovered after brief retry", total_tokens=15),
                ]
            )
            text, tokens = await router.generate([{"role": "user", "content": "hi"}])
            self.assertEqual(text, "Recovered after brief retry")
            self.assertEqual(tokens, 15)
            self.assertEqual(router.primary_client.chat.completions.create.await_count, 2)
        finally:
            await router.aclose()


class HandlerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = Settings.from_env(
            {
                "BOT_TOKEN": "123:abc",
                "AI_API_KEY": "sk-primary",
                "ADMIN_USERS": "999",
                "GROUP_TRIGGER_MODE": "mention_or_reply",
                "DAILY_MSG_LIMIT": "10",
            }
        )
        self.storage = SQLiteStorage(":memory:")
        await self.storage.init()
        self.llm = LLMRouter(self.settings)
        self.search = SearchService(self.settings)
        self.vision = VisionService(self.settings, self.llm.primary_client)
        self.media = MediaService(self.settings, self.llm.primary_client)
        self.services = AppServices(
            settings=self.settings,
            storage=self.storage,
            access=AccessController(self.settings, self.storage),
            rate_limiter=RateLimiter(self.settings, self.storage),
            llm=self.llm,
            search=self.search,
            vision=self.vision,
            media=self.media,
            bot_id=777,
            bot_username="comm_test_bot",
        )

    async def asyncTearDown(self) -> None:
        await self.llm.aclose()
        await self.storage.close()

    def _ctx(self):
        bot = SimpleNamespace(
            id=777,
            username="comm_test_bot",
            send_chat_action=AsyncMock(),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )
        app = SimpleNamespace(bot_data={SERVICES_KEY: self.services})
        return SimpleNamespace(bot=bot, application=app, args=[])

    async def test_dm_flow_search_directive_calc_and_memory_persistence(self) -> None:
        self.search.search_web_live = AsyncMock(  # type: ignore[method-assign]
            return_value="- Official Release (https://docs.python.org/3/): Released recently."
        )
        self.search.verify_citations = AsyncMock(side_effect=lambda txt: txt)  # type: ignore[method-assign]
        self.llm.generate = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                ("[SEARCH: python 3.12 release]", 15),
                ("Answer is [CALC: 6 * 7]. [REMEMBER: Likes Python]", 25),
            ]
        )

        reply_mock = AsyncMock()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=101, first_name="Alex", username="alex", is_bot=False),
            effective_chat=SimpleNamespace(id=101, type="private"),
            effective_message=SimpleNamespace(
                message_id=1,
                text="Tell me about Python",
                caption=None,
                sender_chat=None,
                reply_text=reply_mock,
            ),
        )
        ctx = self._ctx()
        await handle_message(update, ctx)  # type: ignore[arg-type]

        reply_mock.assert_awaited_once()
        sent_html = reply_mock.call_args.args[0]
        self.assertIn("Answer is 42.", sent_html)
        self.assertNotIn("[REMEMBER:", sent_html)

        mem = await self.storage.get_chat_memory("dm:101")
        self.assertEqual(len(mem.messages), 2)
        self.assertEqual(await self.storage.get_user_profile(101), ["Likes Python"])
        usage = await self.storage.get_usage(101)
        self.assertEqual(usage.day_msgs, 1)
        self.assertEqual(usage.day_searches, 1)

    async def test_non_admin_cannot_ban_or_reset_group_history(self) -> None:
        reply_ban = AsyncMock()
        ctx = self._ctx()
        ctx.args = ["101", "spam"]
        non_admin_upd = SimpleNamespace(
            effective_user=SimpleNamespace(id=101, is_bot=False),
            effective_chat=SimpleNamespace(id=101, type="private"),
            effective_message=SimpleNamespace(message_id=2, text="/ban 101", sender_chat=None, reply_text=reply_ban),
        )
        await ban_command(non_admin_upd, ctx)  # type: ignore[arg-type]
        self.assertIn("only configured bot admins", reply_ban.call_args.args[0].lower())
        self.assertFalse(await self.storage.is_banned(101))

        reply_reset = AsyncMock()
        group_upd = SimpleNamespace(
            effective_user=SimpleNamespace(id=101, is_bot=False),
            effective_chat=SimpleNamespace(id=-10099, type="supergroup", is_forum=False),
            effective_message=SimpleNamespace(message_id=3, text="/reset", sender_chat=None, reply_text=reply_reset),
        )
        await reset_command(group_upd, ctx)  # type: ignore[arg-type]
        self.assertIn("only group admins", reply_reset.call_args.args[0].lower())

    async def test_usage_command_shows_user_quotas(self) -> None:
        await self.storage.increment_usage(101, msgs=4, searches=1)
        reply_usage = AsyncMock()
        upd = SimpleNamespace(
            effective_user=SimpleNamespace(id=101, is_bot=False),
            effective_chat=SimpleNamespace(id=101, type="private"),
            effective_message=SimpleNamespace(message_id=4, text="/usage", sender_chat=None, reply_text=reply_usage),
        )
        await usage_command(upd, self._ctx())  # type: ignore[arg-type]
        out = reply_usage.call_args.args[0]
        self.assertIn("4/10", out)
        self.assertIn("1/15", out)

    async def test_admin_ban_rejects_malformed_double_minus_id(self) -> None:
        reply_ban = AsyncMock()
        ctx = self._ctx()
        ctx.args = ["--123"]
        admin_upd = SimpleNamespace(
            effective_user=SimpleNamespace(id=999, is_bot=False),
            effective_chat=SimpleNamespace(id=999, type="private"),
            effective_message=SimpleNamespace(message_id=5, text="/ban --123", sender_chat=None, reply_text=reply_ban),
        )
        await ban_command(admin_upd, ctx)  # type: ignore[arg-type]
        self.assertIn("Usage: /ban", reply_ban.call_args.args[0])

    async def test_extract_first_url_handles_punctuation_and_wikipedia_parens(self) -> None:
        self.assertEqual(extract_first_url("Check https://example.com/docs."), "https://example.com/docs")
        self.assertEqual(extract_first_url("(see https://example.com/page)."), "https://example.com/page")
        self.assertEqual(
            extract_first_url("Read (https://en.wikipedia.org/wiki/Python_(programming_language))."),
            "https://en.wikipedia.org/wiki/Python_(programming_language)",
        )

    async def test_empty_ask_does_not_burn_rate_limit(self) -> None:
        reply_mock = AsyncMock()
        ctx = self._ctx()
        ctx.args = []
        upd = SimpleNamespace(
            effective_user=SimpleNamespace(id=202, is_bot=False),
            effective_chat=SimpleNamespace(id=202, type="private"),
            effective_message=SimpleNamespace(message_id=10, text="/ask", sender_chat=None, reply_text=reply_mock),
        )
        for _ in range(5):
            await ask_command(upd, ctx)  # type: ignore[arg-type]
        self.assertEqual(reply_mock.await_count, 5)
        self.assertTrue(all("Usage: /ask" in call.args[0] for call in reply_mock.call_args_list))
        self.assertTrue(self.services.rate_limiter.check_rate_limit(202).allowed)

    async def test_disabled_feature_directive_only_reply_sends_fallback_not_silence(self) -> None:
        s_no_img = Settings.from_env(
            {
                "BOT_TOKEN": "123:abc",
                "AI_API_KEY": "sk-primary",
                "ENABLE_IMAGE_GEN": "false",
            }
        )
        self.services.settings = s_no_img
        self.services.access = AccessController(s_no_img, self.storage)
        self.services.rate_limiter = RateLimiter(s_no_img, self.storage)
        self.llm.generate = AsyncMock(return_value=("[IMAGE: cute cat]", 10))  # type: ignore[method-assign]

        reply_mock = AsyncMock()
        upd = SimpleNamespace(
            effective_user=SimpleNamespace(id=303, first_name="Sam", username="sam", is_bot=False),
            effective_chat=SimpleNamespace(id=303, type="private"),
            effective_message=SimpleNamespace(
                message_id=11,
                text="draw a cat",
                caption=None,
                sender_chat=None,
                reply_text=reply_mock,
            ),
        )
        await handle_message(upd, self._ctx())  # type: ignore[arg-type]
        reply_mock.assert_awaited_once()
        self.assertIn("couldn't generate output", reply_mock.call_args.args[0].lower())

    async def test_send_chat_action_safe_supports_forum_thread_and_ignores_errors(self) -> None:
        ctx = self._ctx()
        forum_upd = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-10055, is_forum=True),
            effective_message=SimpleNamespace(message_thread_id=88),
        )
        await send_chat_action_safe(forum_upd, ctx)  # type: ignore[arg-type]
        ctx.bot.send_chat_action.assert_awaited_once()
        self.assertEqual(ctx.bot.send_chat_action.call_args.kwargs.get("message_thread_id"), 88)

        ctx.bot.send_chat_action = AsyncMock(side_effect=RuntimeError("network blip"))
        await send_chat_action_safe(forum_upd, ctx)  # type: ignore[arg-type]

    async def test_error_handler_handles_conflict_retry_and_timeout_cleanly(self) -> None:
        from telegram.error import Conflict, RetryAfter, TimedOut

        for err in (Conflict("other instance"), RetryAfter(5), TimedOut("slow")):
            ctx = SimpleNamespace(error=err)
            await error_handler(None, ctx)  # type: ignore[arg-type]

    def test_create_application_configures_resilient_timeouts(self) -> None:
        from bot import create_application

        app = create_application(self.settings)
        self.assertGreaterEqual(app.bot.request._client_kwargs["timeout"].read, 20.0)
