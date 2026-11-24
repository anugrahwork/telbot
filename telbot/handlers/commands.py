"""User and Admin Telegram slash-command handlers."""

from __future__ import annotations

import hashlib
import html
import re
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from telbot.handlers.context import get_services
from telbot.handlers.messages import ask_and_reply, guard_and_acquire, send_chat_action_safe


_INT_ID_RE = re.compile(r"-?\d+")


def _progress_bar(used: int, limit: int, width: int = 10) -> str:
    if limit <= 0:
        return "Unlimited"
    ratio = min(1.0, max(0.0, used / limit))
    filled = round(ratio * width)
    return f"{'█' * filled}{'░' * (width - filled)} {used}/{limit}"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    allowed, denial = await svc.access.check_access(update, context)
    if not allowed:
        if denial:
            await msg.reply_text(denial)
        return

    is_group = svc.access.is_group_chat(update)
    is_admin = svc.access.is_admin(user.id)

    features = ["Ask questions & get structured answers (with Smart Hybrid Tables)"]
    if svc.settings.enable_web_search:
        features.append("Live paywall-skipping search via /search <query>")
    if svc.settings.enable_url_reader:
        features.append("Public link summaries (paste a URL)")
    if svc.settings.enable_vision and svc.vision.has_any_engine:
        features.append("Photo & screenshot analysis")
    if svc.settings.enable_file_gen:
        features.append("Downloadable PDF, Word (.docx), and code/CSV generation")
    if svc.settings.enable_image_gen:
        features.append("AI image generation")

    group_hint = ""
    if is_group and svc.settings.group_trigger_mode == "mention_or_reply":
        uname = svc.bot_username or "this bot"
        group_hint = f"In groups, mention @{uname}, reply to my message, or use /ask <question>."

    lines = [
        "Hey! I'm Telbot, your Telegram AI assistant.",
        "",
        "What I can do:",
        *(f"• {f}" for f in features),
    ]
    if group_hint:
        lines.extend(["", group_hint])
    lines.extend(
        [
            "",
            "Commands:",
            "/ask <question> - Ask in a group or DM",
            "/search <topic> - Live accessible web search",
            "/usage - View your daily quota & usage",
            "/reset - Clear current conversation memory",
            "/id - Show your Telegram & chat ID",
            "/privacy - Data storage & retention policy",
            "/help - Show help",
        ]
    )
    if is_admin:
        lines.extend(
            [
                "",
                "Admin Commands:",
                "/model - Switch active AI model",
                "/ban <id> [reason] - Block user or group",
                "/unban <id> - Unblock user or group",
                "/admin - Bot stats & maintenance",
            ]
        )
    await msg.reply_text("\n".join(line for line in lines if line is not None))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_command(update, context)


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat:
        return
    if await svc.access.is_banned(user.id, chat.id if svc.access.is_group_chat(update) else None):
        return

    is_admin = svc.access.is_admin(user.id)
    role_label = "Admin" if is_admin else "Community Member"
    if svc.access.is_group_chat(update):
        if not is_admin and await svc.access.leave_if_disallowed_group(update, context):
            return
        thread_id = getattr(msg, "message_thread_id", None)
        thread_line = f"\nForum Topic ID: <code>{thread_id}</code>" if thread_id else ""
        await msg.reply_text(
            f"Your User ID: <code>{user.id}</code> ({role_label})\n"
            f"Group Chat ID: <code>{chat.id}</code>{thread_line}",
            parse_mode=ParseMode.HTML,
        )
        return

    await msg.reply_text(
        f"Your User ID: <code>{user.id}</code> ({role_label})",
        parse_mode=ParseMode.HTML,
    )


async def privacy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    msg = update.effective_message
    if not msg:
        return
    allowed, denial = await svc.access.check_access(update, context)
    if not allowed:
        if denial:
            await msg.reply_text(denial)
        return

    provider_host = urlparse(svc.settings.ai_base_url).hostname or "configured AI provider"
    retention_line = (
        f"Inactive conversation threads auto-expire after {svc.settings.history_retention_days} days."
        if svc.settings.history_retention_days > 0
        else "Automatic idle pruning is disabled by the operator."
    )
    storage_desc = "SQLite + optional Supabase sync" if svc.settings.has_supabase else "local SQLite database"

    await msg.reply_text(
        "Privacy & Data Handling:\n"
        f"• Storage: Recent conversation context and daily quota counters are saved in {storage_desc}.\n"
        f"• Retention: {retention_line}\n"
        "• Your controls: Run /reset anytime to erase chat context (or /reset all in DM to wipe profile notes too).\n"
        f"• External processing: Prompts are sent to {provider_host} to generate replies"
        + (" and web queries may use DuckDuckGo/Crossref/Tavily." if svc.settings.enable_web_search else ".")
    )


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    allowed, denial = await svc.access.check_access(update, context)
    if not allowed:
        if denial:
            await msg.reply_text(denial)
        return

    usage = await svc.storage.get_usage(user.id)
    is_admin = svc.access.is_admin(user.id)
    bypass = is_admin and svc.settings.admin_bypass_limits

    def _fmt(used: int, limit: int) -> str:
        if bypass:
            return f"{used} used (Admin unlimited)"
        if limit <= 0:
            return f"{used} used (Unlimited)"
        return _progress_bar(used, limit)

    lines = [
        "Your Usage & Daily Quotas (resets 00:00 UTC):",
        f"• Messages: {_fmt(usage.day_msgs, svc.settings.daily_msg_limit)}",
        f"• Searches: {_fmt(usage.day_searches, svc.settings.daily_search_limit)}",
        f"• Images: {_fmt(usage.day_images, svc.settings.daily_image_limit)}",
        f"• Files/Photos: {_fmt(usage.day_files, svc.settings.daily_file_limit)}",
        f"• This Month: {usage.month_msgs} msgs (~{usage.month_tokens:,} tokens)",
    ]

    if is_admin:
        pref = await svc.storage.get_global_model()
        active = f"{pref[1]} ({pref[0]})" if pref else f"Auto ({svc.settings.ai_model})"
        lines.extend(
            [
                "",
                "Admin Diagnostics:",
                f"• Active Model: {active}",
                f"• Provider Health: {svc.llm.health_summary()}",
            ]
        )

    await msg.reply_text("\n".join(lines))


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    allowed, denial = await svc.access.check_access(update, context)
    if not allowed:
        if denial:
            await msg.reply_text(denial)
        return

    is_group = svc.access.is_group_chat(update)
    if is_group and not await svc.access.is_telegram_group_admin(update, context):
        await msg.reply_text("Only group admins or bot admins can reset shared group memory.")
        return

    scope_key = svc.access.memory_scope_key(update)
    await svc.storage.clear_chat_memory(scope_key)

    wipe_all = bool(context.args and context.args[0].lower() == "all")
    if not is_group and wipe_all:
        await svc.storage.clear_user_profile(user.id)
        await msg.reply_text("Cleared your DM conversation history AND saved profile notes.")
        return

    label = "Group" if is_group else "DM"
    hint = "" if is_group else " (Use /reset all to wipe saved profile notes too.)"
    await msg.reply_text(f"{label} conversation memory cleared!{hint}")


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    msg = update.effective_message
    if not msg:
        return
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        allowed, denial = await svc.access.check_access(update, context)
        if not allowed:
            if denial:
                await msg.reply_text(denial)
            return
        await msg.reply_text("Usage: /ask <your question>")
        return

    ok, uid = await guard_and_acquire(update, context, quota_kinds=("msgs",), check_group_trigger=False)
    if not ok or uid is None:
        return

    try:
        await send_chat_action_safe(update, context, ChatAction.TYPING)
        await ask_and_reply(update, context, query)
    finally:
        svc.rate_limiter.release_user_slot(uid)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    msg = update.effective_message
    if not msg:
        return
    if not svc.settings.enable_web_search:
        allowed, denial = await svc.access.check_access(update, context)
        if not allowed:
            if denial:
                await msg.reply_text(denial)
            return
        await msg.reply_text("Live web search is disabled on this bot instance.")
        return

    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        allowed, denial = await svc.access.check_access(update, context)
        if not allowed:
            if denial:
                await msg.reply_text(denial)
            return
        await msg.reply_text("Usage: /search <what you want to look up>")
        return

    ok, uid = await guard_and_acquire(update, context, quota_kinds=("msgs", "searches"), check_group_trigger=False)
    if not ok or uid is None:
        return

    try:
        await send_chat_action_safe(update, context, ChatAction.TYPING)
        results = await svc.search.search_web_live(query)
        extra = f"Live web search results for '{query}':\n{results}" if results else ""
        await ask_and_reply(
            update,
            context,
            query,
            extra,
            extra_label="web search",
            used_search=True,
            search_attempted=True,
        )
    finally:
        svc.rate_limiter.release_user_slot(uid)


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if not svc.access.is_admin(user.id):
        await msg.reply_text("Only configured bot admins can switch models.")
        return

    current = await svc.storage.get_global_model()
    current_label = f"{current[1]} ({current[0]})" if current else f"Auto ({svc.settings.ai_model})"

    choices = await svc.llm.list_available_models()
    if len(svc.model_button_cache) > 200:
        svc.model_button_cache.clear()
    buttons = [[InlineKeyboardButton("Auto (Primary + Fallback)", callback_data="mdl:auto")]]
    for prov_key, model_id, tag in choices:
        key = hashlib.md5(f"{prov_key}:{model_id}".encode("utf-8")).hexdigest()[:10]
        svc.model_button_cache[key] = (prov_key, model_id)
        mark = "[Active] " if current == (prov_key, model_id) else ""
        buttons.append([InlineKeyboardButton(f"{mark}{model_id} [{tag}]", callback_data=f"mdl:{key}")])

    await msg.reply_text(
        f"Active model: {current_label}\nTap a model below to switch globally:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    query = update.callback_query
    if not query or not query.data:
        return
    if not svc.access.is_admin(query.from_user.id):
        await query.answer("Only configured bot admins can change models.", show_alert=True)
        return

    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "auto":
        await svc.storage.set_global_model(None)
        label = f"Auto ({svc.settings.ai_model})"
    elif choice in svc.model_button_cache:
        prov_key, model_id = svc.model_button_cache[choice]
        await svc.storage.set_global_model((prov_key, model_id))
        label = f"{model_id} ({prov_key})"
    else:
        await query.edit_message_text("That menu expired — send /model again.")
        return

    await query.edit_message_text(f"Switched active model to: {label}")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not svc.access.is_admin(user.id):
        if msg:
            await msg.reply_text("Only configured bot admins can run /ban.")
        return
    if not context.args or not _INT_ID_RE.fullmatch(context.args[0].strip()):
        await msg.reply_text("Usage: /ban <user_or_group_id> [reason]")
        return

    target_id = int(context.args[0])
    if svc.access.is_admin(target_id):
        await msg.reply_text("Cannot ban a configured admin.")
        return
    reason = " ".join(context.args[1:]).strip() or "Banned by admin"
    await svc.storage.ban_target(target_id, reason, user.id)
    await msg.reply_text(f"Banned ID <code>{target_id}</code> ({html.escape(reason)}).", parse_mode=ParseMode.HTML)


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not svc.access.is_admin(user.id):
        if msg:
            await msg.reply_text("Only configured bot admins can run /unban.")
        return
    if not context.args or not _INT_ID_RE.fullmatch(context.args[0].strip()):
        await msg.reply_text("Usage: /unban <user_or_group_id>")
        return

    target_id = int(context.args[0])
    removed = await svc.storage.unban_target(target_id)
    if removed:
        await msg.reply_text(f"Unbanned ID <code>{target_id}</code>.", parse_mode=ParseMode.HTML)
    else:
        await msg.reply_text(f"ID <code>{target_id}</code> was not in the dynamic ban list.", parse_mode=ParseMode.HTML)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_services(context)
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not svc.access.is_admin(user.id):
        if msg:
            await msg.reply_text("Only configured bot admins can view /admin diagnostics.")
        return

    sub = context.args[0].lower() if context.args else "status"
    if sub == "prune":
        removed = await svc.storage.prune_inactive(svc.settings.history_retention_days)
        await msg.reply_text(
            f"Pruned {removed} inactive records older than {svc.settings.history_retention_days} days."
        )
        return

    if sub == "bans":
        bans = await svc.storage.list_bans()
        if not bans:
            await msg.reply_text("No dynamic bans recorded.")
            return
        lines = [f"• {tid}: {reason} (by {by})" for tid, reason, by, _ in bans[:25]]
        await msg.reply_text("Dynamic Bans:\n" + "\n".join(lines))
        return

    stats = await svc.storage.get_stats()
    mode = "Webhook" if svc.settings.webhook_url else "Local Polling"
    await msg.reply_text(
        "Telbot Admin Status:\n"
        f"• Mode: {mode}\n"
        f"• Group Trigger: {svc.settings.group_trigger_mode}\n"
        f"• Rate Limits: {svc.settings.rate_limit_burst}/{svc.settings.rate_limit_burst_window}s burst, "
        f"{svc.settings.rate_limit_per_minute}/min user, {svc.settings.group_rate_limit_per_minute}/min group\n"
        f"• Daily Quotas: {svc.settings.daily_msg_limit} msgs, {svc.settings.daily_search_limit} searches, "
        f"{svc.settings.daily_image_limit} images, {svc.settings.daily_file_limit} files\n"
        f"• Provider Health: {svc.llm.health_summary()}\n"
        f"• DB Stats: {stats['scopes']} scopes, {stats['profiles']} profiles, "
        f"{stats['tracked_users']} users, {stats['banned_targets']} bans\n\n"
        "Subcommands: /admin bans, /admin prune"
    )
