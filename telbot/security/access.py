"""Access control, fail-closed admin checks, group trigger policy, and ban enforcement."""

from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.ext import ContextTypes

from telbot.config import Settings
from telbot.storage.base import StorageBackend

logger = logging.getLogger(__name__)

_ANONYMOUS_ADMIN_BOT_ID = 1087968824


class AccessController:
    """Evaluates DM/group policies, fail-closed admin checks, bans, and group triggers."""

    def __init__(self, settings: Settings, storage: StorageBackend) -> None:
        self.settings = settings
        self.storage = storage

    def is_admin(self, user_id: int | None) -> bool:
        """Fail-closed admin check: if ADMIN_USERS is empty, returns False for everyone."""
        return user_id is not None and user_id in self.settings.admin_users

    @staticmethod
    def is_group_chat(update: Update) -> bool:
        chat = update.effective_chat
        return bool(chat and getattr(chat, "type", None) in {"group", "supergroup"})

    @classmethod
    def is_anonymous_group_admin(cls, update: Update) -> bool:
        msg = getattr(update, "effective_message", None)
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        sender_chat = getattr(msg, "sender_chat", None)
        return bool(
            cls.is_group_chat(update)
            and chat
            and user
            and user.id == _ANONYMOUS_ADMIN_BOT_ID
            and sender_chat
            and getattr(sender_chat, "id", None) == chat.id
        )

    async def is_telegram_group_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE | None) -> bool:
        """Return True if caller is a bot admin, anonymous group admin, or Telegram group admin."""
        user = update.effective_user
        if self.is_admin(user.id if user else None) or self.is_anonymous_group_admin(update):
            return True
        chat = update.effective_chat
        if not (self.is_group_chat(update) and chat and user and context and getattr(context, "bot", None)):
            return False
        try:
            member = await context.bot.get_chat_member(chat.id, user.id)
            return getattr(member, "status", "") in {"administrator", "creator"}
        except Exception as exc:
            logger.debug("Could not verify Telegram group admin status in %s: %s", chat.id, exc)
            return False

    @classmethod
    def memory_scope_key(cls, update: Update) -> str:
        """Scope memory per DM user, or per group chat (and forum topic thread if active)."""
        if cls.is_group_chat(update) and update.effective_chat:
            chat_id = update.effective_chat.id
            msg = update.effective_message
            thread_id = getattr(msg, "message_thread_id", None)
            if thread_id and getattr(update.effective_chat, "is_forum", False):
                return f"group:{chat_id}:topic:{thread_id}"
            return f"group:{chat_id}"
        uid = update.effective_user.id if update.effective_user else 0
        return f"dm:{uid}"

    @staticmethod
    def strip_bot_mention(text: str, bot_username: str | None) -> str:
        if not bot_username:
            return text
        uname = bot_username.lstrip("@")
        if not uname:
            return text
        return re.sub(rf"@{re.escape(uname)}\b[:,]?\s*", "", text, flags=re.IGNORECASE).strip()

    def should_respond_in_group(
        self,
        update: Update,
        bot_username: str | None,
        bot_id: int | None = None,
    ) -> bool:
        """Check whether the bot should respond to a non-command group message."""
        if not self.is_group_chat(update):
            return True
        if self.settings.group_trigger_mode == "all":
            return True

        msg = update.effective_message
        if not msg:
            return False

        reply_to = getattr(msg, "reply_to_message", None)
        if reply_to and getattr(reply_to, "from_user", None):
            if bot_id is not None and reply_to.from_user.id == bot_id:
                return True
            if bot_username and (getattr(reply_to.from_user, "username", "") or "").lower() == bot_username.lstrip("@").lower():
                return True

        text = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
        if bot_username:
            uname = bot_username.lstrip("@")
            if uname and re.search(rf"(?<!\w)@{re.escape(uname)}\b", text, flags=re.IGNORECASE):
                return True
        return False

    async def is_banned(self, user_id: int | None, chat_id: int | None = None) -> bool:
        """Check static env blocklists and dynamic SQLite ban table (admins cannot be locked out)."""
        if user_id is not None and self.is_admin(user_id):
            return False
        if user_id is not None and user_id in self.settings.blocked_users:
            return True
        if chat_id is not None and chat_id in self.settings.blocked_groups:
            return True
        if user_id is not None and await self.storage.is_banned(user_id):
            return True
        if chat_id is not None and await self.storage.is_banned(chat_id):
            return True
        return False

    async def leave_if_disallowed_group(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE | None = None,
    ) -> bool:
        """Auto-leave groups that are blocked, disabled, or outside ALLOWED_GROUPS."""
        if not self.is_group_chat(update):
            return False
        chat = update.effective_chat
        if not chat:
            return False

        disallowed = (
            not self.settings.allow_groups
            or chat.id in self.settings.blocked_groups
            or (bool(self.settings.allowed_groups) and chat.id not in self.settings.allowed_groups)
            or await self.storage.is_banned(chat.id)
        )
        if not disallowed:
            return False

        logger.warning("Leaving disallowed/unauthorized group %s", chat.id)
        if context and getattr(context, "bot", None):
            try:
                await context.bot.leave_chat(chat.id)
            except Exception as exc:
                logger.debug("Could not leave group %s: %s", chat.id, exc)
        return True

    async def check_access(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE | None = None,
    ) -> tuple[bool, str]:
        """Return (allowed, denial_message) for an incoming user update."""
        user = update.effective_user
        is_anon_admin = self.is_anonymous_group_admin(update)
        if not user or (getattr(user, "is_bot", False) and not is_anon_admin):
            return False, ""

        chat = update.effective_chat
        chat_id = chat.id if (self.is_group_chat(update) and chat) else None

        if await self.is_banned(user.id, chat_id):
            return False, ""

        if self.is_group_chat(update):
            if await self.leave_if_disallowed_group(update, context):
                return False, ""
            return True, ""

        if self.settings.allow_dms or self.is_admin(user.id):
            return True, ""

        return False, "Direct messages are disabled by the bot administrator."
