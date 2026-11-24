"""Shared dependency container and request deduplication for Telegram handlers."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from telegram.ext import ContextTypes

from telbot.config import Settings
from telbot.security.access import AccessController
from telbot.security.rate_limit import RateLimiter
from telbot.services.llm import LLMRouter
from telbot.services.media import MediaService
from telbot.services.search import SearchService
from telbot.services.vision import VisionService
from telbot.storage.base import StorageBackend

SERVICES_KEY = "telbot_services"


@dataclass
class AppServices:
    """Application-scoped dependency container stored in bot_data."""

    settings: Settings
    storage: StorageBackend
    access: AccessController
    rate_limiter: RateLimiter
    llm: LLMRouter
    search: SearchService
    vision: VisionService
    media: MediaService

    bot_id: int | None = None
    bot_username: str | None = None
    model_button_cache: dict[str, tuple[str, str]] = field(default_factory=dict)
    _seen_order: deque[tuple[int, int]] = field(default_factory=deque)
    _seen_set: set[tuple[int, int]] = field(default_factory=set)

    def mark_message_seen(self, chat_id: int, message_id: int) -> bool:
        """Return True if (chat_id, message_id) is new, or False if it is a duplicate delivery."""
        key = (chat_id, message_id)
        if key in self._seen_set:
            return False
        self._seen_set.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > 500:
            old = self._seen_order.popleft()
            self._seen_set.discard(old)
        return True


def get_services(context: ContextTypes.DEFAULT_TYPE) -> AppServices:
    """Retrieve AppServices from PTB's bot_data."""
    services = context.application.bot_data.get(SERVICES_KEY)
    if not isinstance(services, AppServices):
        raise RuntimeError("AppServices not initialized in application.bot_data")
    return services
