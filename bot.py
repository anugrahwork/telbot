"""Entrypoint for Telbot — an open-access community Telegram AI bot."""

from __future__ import annotations

import asyncio
import logging
import sys

from telegram import (
    BotCommandScopeAllGroupChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    Update,
)
from telegram.error import Conflict, InvalidToken, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from telbot.config import ConfigError, Settings
from telbot.handlers import (
    SERVICES_KEY,
    AppServices,
    admin_command,
    ask_command,
    ban_command,
    error_handler,
    handle_document,
    handle_message,
    handle_my_chat_member,
    handle_photo,
    help_command,
    id_command,
    model_callback,
    model_command,
    privacy_command,
    reset_command,
    run_webhook_server,
    search_command,
    start_command,
    unban_command,
    usage_command,
)
from telbot.security.access import AccessController
from telbot.security.rate_limit import RateLimiter
from telbot.services.llm import LLMRouter
from telbot.services.media import MediaService
from telbot.services.search import SearchService
from telbot.services.vision import VisionService
from telbot.storage import SQLiteStorage, StorageBackend, SupabaseStorage

logger = logging.getLogger(__name__)


def build_storage(settings: Settings) -> StorageBackend:
    sqlite_store = SQLiteStorage(settings.db_path)
    if settings.has_supabase:
        return SupabaseStorage(sqlite_store, settings.supabase_url, settings.supabase_key)
    return sqlite_store


def build_services(settings: Settings, storage: StorageBackend) -> AppServices:
    llm = LLMRouter(settings)
    return AppServices(
        settings=settings,
        storage=storage,
        access=AccessController(settings, storage),
        rate_limiter=RateLimiter(settings, storage),
        llm=llm,
        search=SearchService(settings),
        vision=VisionService(settings, llm.primary_client),
        media=MediaService(settings, llm.primary_client),
    )


async def post_init(app: Application) -> None:
    svc: AppServices = app.bot_data[SERVICES_KEY]
    await svc.storage.init()
    pruned = await svc.storage.prune_inactive(svc.settings.history_retention_days)
    if pruned:
        logger.info("Startup retention cleanup removed %s expired records", pruned)

    me = None
    try:
        me = app.bot.bot
    except Exception:
        me = None
    if me is None and hasattr(app.bot, "get_me"):
        try:
            me = await app.bot.get_me()
        except Exception as exc:
            logger.warning("Could not inspect bot profile via get_me(): %s", exc)

    if me is not None:
        svc.bot_id = me.id
        svc.bot_username = me.username
        can_read_all = getattr(me, "can_read_all_group_messages", None)
        logger.info(
            "Telbot ready: @%s (id=%s) | trigger=%s | admins=%d | allowed_groups=%s",
            me.username,
            me.id,
            svc.settings.group_trigger_mode,
            len(svc.settings.admin_users),
            sorted(svc.settings.allowed_groups) or "ALL",
        )
        if svc.settings.group_trigger_mode == "all" and can_read_all is False:
            logger.warning(
                "GROUP_TRIGGER_MODE=all is set, but @BotFather Group Privacy Mode is still ENABLED. "
                "In @BotFather run /setprivacy -> @%s -> Disable, or switch GROUP_TRIGGER_MODE=mention_or_reply.",
                me.username,
            )

    public_commands = [
        ("start", "Overview & features"),
        ("ask", "Ask the AI assistant"),
        ("search", "Live accessible web search"),
        ("usage", "Your daily quota & usage"),
        ("reset", "Clear conversation memory"),
        ("id", "Show user & chat ID"),
        ("privacy", "Data & retention policy"),
        ("help", "Show help"),
    ]
    admin_commands = public_commands + [
        ("model", "Switch active AI model (Admin)"),
        ("ban", "Ban user or group ID (Admin)"),
        ("unban", "Unban user or group ID (Admin)"),
        ("admin", "Bot stats & maintenance (Admin)"),
    ]
    try:
        await app.bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(public_commands, scope=BotCommandScopeAllGroupChats())
    except Exception as exc:
        logger.warning("Could not register default command menu: %s", exc)

    for aid in sorted(svc.settings.admin_users):
        try:
            await app.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=aid))
        except Exception as exc:
            logger.debug("Could not register admin menu for %s: %s", aid, exc)


async def post_shutdown(app: Application) -> None:
    svc: AppServices | None = app.bot_data.get(SERVICES_KEY)
    if svc is not None:
        await svc.llm.aclose()
        await svc.storage.close()


def create_application(settings: Settings) -> Application:
    storage = build_storage(settings)
    services = build_services(settings, storage)

    app = (
        Application.builder()
        .token(settings.bot_token)
        .connect_timeout(15.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(10.0)
        .get_updates_connect_timeout(15.0)
        .get_updates_read_timeout(30.0)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data[SERVICES_KEY] = services

    app.add_error_handler(error_handler)
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("ask", ask_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("usage", usage_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("privacy", privacy_command))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r"^mdl:"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=getattr(logging, settings.log_level, logging.INFO),
    )
    for noisy_logger in ("httpx", "httpx2", "httpcore", "httpcore2", "apscheduler"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    app = create_application(settings)

    try:
        if settings.webhook_url:
            asyncio.run(run_webhook_server(app, settings))
        else:
            logger.info("Starting local polling mode... press Ctrl+C to stop")
            asyncio.set_event_loop(asyncio.new_event_loop())
            app.run_polling(
                drop_pending_updates=True,
                bootstrap_retries=1,
                allowed_updates=Update.ALL_TYPES,
            )
    except KeyboardInterrupt:
        pass
    except InvalidToken:
        logger.error("Invalid BOT_TOKEN in .env — check the token from @BotFather and try again.")
        raise SystemExit(1) from None
    except Conflict:
        logger.error(
            "Telegram Conflict: another instance or webhook is already active for this BOT_TOKEN. "
            "Stop the other instance or use a fresh token from @BotFather."
        )
        raise SystemExit(1) from None
    except RetryAfter as exc:
        logger.error(
            "Telegram rejected or rate-limited BOT_TOKEN (429, retry after %ss). "
            "Verify your exact token in @BotFather (/mybots -> API Token) and update .env, "
            "and make sure no other instance shares it.",
            getattr(exc, "retry_after", 15),
        )
        raise SystemExit(1) from None
    except (TimedOut, NetworkError) as exc:
        logger.error(
            "Could not complete Telegram Bot API startup (%s). "
            "Check your internet/VPN/firewall and verify BOT_TOKEN in @BotFather (/mybots -> API Token).",
            exc,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
