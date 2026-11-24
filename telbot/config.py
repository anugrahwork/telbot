"""Typed configuration loader for Telbot."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PLACEHOLDER_TOKENS = {
    "",
    "your_telegram_bot_token_here",
    "your_bot_token_here",
    "your_api_key_here",
    "your_primary_api_key_here",
    "sk-your-key-here",
    "replace_me",
    "changeme",
}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _clean_val(raw: str | None, default: str = "") -> str:
    val = (raw if raw is not None else default) or default
    return val.strip().strip("'\"")


def _is_placeholder(val: str) -> bool:
    low = val.strip().lower()
    if low in _PLACEHOLDER_TOKENS:
        return True
    return low.startswith("your_") and low.endswith("_here")


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    val = raw.strip().lower()
    if val in {"1", "true", "yes", "on", "y"}:
        return True
    if val in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _parse_int(raw: str | None, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if raw is None or not raw.strip():
        val = default
    else:
        try:
            val = int(raw.strip())
        except ValueError:
            val = default
    if minimum is not None:
        val = max(minimum, val)
    if maximum is not None:
        val = min(maximum, val)
    return val


def _parse_float(raw: str | None, default: float, *, minimum: float | None = None) -> float:
    if raw is None or not raw.strip():
        val = default
    else:
        try:
            val = float(raw.strip())
        except ValueError:
            val = default
    if minimum is not None:
        val = max(minimum, val)
    return val


_INT_ID_RE = re.compile(r"-?\d+")


def _parse_id_set(raw: str | None) -> frozenset[int]:
    if not raw or not raw.strip():
        return frozenset()
    ids: set[int] = set()
    for part in raw.split(","):
        item = part.strip()
        if item and _INT_ID_RE.fullmatch(item):
            ids.add(int(item))
    return frozenset(ids)


def _parse_ports(raw: str | None, default: tuple[int, ...] = (80, 443)) -> tuple[int, ...]:
    if not raw or not raw.strip():
        return default
    ports: list[int] = []
    for part in raw.split(","):
        item = part.strip()
        if item.isdigit():
            p = int(item)
            if 1 <= p <= 65535 and p not in ports:
                ports.append(p)
    return tuple(ports) if ports else default


def _parse_model_list(raw: str | None, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Parse a comma-separated model list, trimming blanks and deduplicating in order."""
    if not raw or not raw.strip():
        return default
    models: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if item and item not in models:
            models.append(item)
    return tuple(models) if models else default


def _normalize_webhook(
    explicit_url: str,
    render_host: str,
    railway_domain: str,
) -> tuple[str, str, tuple[str, ...]]:
    """Resolve effective webhook URL, webhook path, and SSRF-blocked self-hosts."""
    blocked_hosts: list[str] = []

    def _extract_host(raw: str) -> str:
        s = raw.strip()
        if not s:
            return ""
        if "://" in s:
            return (urlparse(s).hostname or "").lower().rstrip(".")
        return s.split("/", 1)[0].split(":", 1)[0].lower().rstrip(".")

    for candidate in (explicit_url, render_host, railway_domain):
        h = _extract_host(candidate)
        if h and h not in blocked_hosts:
            blocked_hosts.append(h)

    if explicit_url:
        parsed = urlparse(explicit_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            path = parsed.path or "/webhook"
            if not path.startswith("/"):
                path = f"/{path}"
            base = f"{parsed.scheme}://{parsed.netloc}{path}"
            return base, path, tuple(blocked_hosts)

    for host_raw in (render_host, railway_domain):
        if host_raw:
            host = _extract_host(host_raw)
            if host:
                return f"https://{host}/webhook", "/webhook", tuple(blocked_hosts)

    return "", "/webhook", tuple(blocked_hosts)


@dataclass(frozen=True)
class Settings:
    """Immutable application settings loaded from environment variables."""

    # Required credentials
    bot_token: str
    ai_api_key: str

    # Primary AI provider (OpenAI-compatible)
    ai_base_url: str
    ai_model: str
    ai_models: tuple[str, ...]
    ai_timeout_seconds: float

    # Optional fallback AI provider
    fallback_ai_api_key: str
    fallback_ai_base_url: str
    fallback_ai_model: str
    fallback_ai_models: tuple[str, ...]

    # Optional dedicated vision / image generation models on primary OpenAI-compatible API
    vision_model: str
    image_gen_model: str

    # Optional third-party integrations
    tavily_api_key: str
    cf_account_id: str
    cf_api_token: str
    cf_llm_model: str
    ocr_api_key: str
    supabase_url: str
    supabase_key: str

    # Access control & group etiquette (fail-closed admin)
    admin_users: frozenset[int]
    allow_dms: bool
    allow_groups: bool
    allowed_groups: frozenset[int]
    blocked_users: frozenset[int]
    blocked_groups: frozenset[int]
    group_trigger_mode: str  # "mention_or_reply" | "all"

    # Rate limiting & quotas
    rate_limit_burst: int
    rate_limit_burst_window: int
    rate_limit_per_minute: int
    group_rate_limit_per_minute: int
    daily_msg_limit: int
    daily_search_limit: int
    daily_image_limit: int
    daily_file_limit: int
    admin_bypass_limits: bool

    # Resource bounds & retention
    max_input_chars: int
    max_file_bytes: int
    max_url_bytes: int
    max_history_messages: int
    history_retention_days: int

    # Feature flags
    enable_web_search: bool
    enable_url_reader: bool
    enable_jina_reader: bool
    enable_vision: bool
    enable_ocr_space: bool
    enable_file_gen: bool
    enable_image_gen: bool
    enable_pollinations_fallback: bool
    enable_charts: bool
    enable_dm_profile_memory: bool

    # Storage & networking
    data_dir: str
    db_path: str
    allowed_web_ports: tuple[int, ...]
    webhook_url: str
    webhook_path: str
    webhook_secret: str
    blocked_ssrf_hosts: tuple[str, ...]
    port: int
    log_level: str

    @property
    def effective_webhook_secret(self) -> str:
        """Return explicit WEBHOOK_SECRET or a deterministic secret derived from BOT_TOKEN."""
        if self.webhook_secret:
            return self.webhook_secret
        return hashlib.sha256(f"telbot:{self.bot_token}".encode("utf-8")).hexdigest()[:32]

    @property
    def has_cloudflare(self) -> bool:
        return bool(self.cf_account_id and self.cf_api_token)

    @property
    def has_supabase(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        require_secrets: bool = True,
    ) -> "Settings":
        """Load settings from a mapping or environment/.env (cloud env vars override .env)."""
        if env is None:
            env_path = os.path.join(_PROJECT_ROOT, ".env")
            load_dotenv(env_path, override=False)
            src: Mapping[str, str] = os.environ
        else:
            src = env

        def get(key: str, default: str = "") -> str:
            return _clean_val(src.get(key), default)

        bot_token = get("BOT_TOKEN")
        if require_secrets and _is_placeholder(bot_token):
            raise ConfigError(
                "Missing valid BOT_TOKEN. Copy .env.example to .env and set BOT_TOKEN from @BotFather."
            )

        ai_api_key = get("AI_API_KEY")
        if require_secrets and _is_placeholder(ai_api_key):
            raise ConfigError(
                "Missing valid AI_API_KEY. Set AI_API_KEY in .env (or your hosting provider's environment variables)."
            )

        port_raw = get("PORT", "10000")
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ConfigError(f"Invalid PORT '{port_raw}'; expected an integer between 1 and 65535.") from exc
        if not (1 <= port <= 65535):
            raise ConfigError(f"Invalid PORT {port}; expected 1..65535.")

        raw_trigger = get("GROUP_TRIGGER_MODE", "mention_or_reply").lower()
        group_trigger_mode = raw_trigger if raw_trigger in {"mention_or_reply", "all"} else "mention_or_reply"

        default_data_dir = os.path.join(_PROJECT_ROOT, "data")
        data_dir = get("DATA_DIR", default_data_dir) or default_data_dir
        db_path = get("DB_PATH") or os.path.join(data_dir, "bot.db")

        webhook_url, webhook_path, blocked_hosts = _normalize_webhook(
            get("WEBHOOK_URL"),
            get("RENDER_EXTERNAL_HOSTNAME"),
            get("RAILWAY_PUBLIC_DOMAIN"),
        )

        max_hist = _parse_int(src.get("MAX_HISTORY_MESSAGES"), 30, minimum=2, maximum=100)
        if max_hist % 2 != 0:
            max_hist += 1

        log_level = get("LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            log_level = "INFO"

        ai_models = _parse_model_list(src.get("AI_MODEL"), ("gpt-4o-mini",))
        fallback_ai_models = _parse_model_list(src.get("FALLBACK_AI_MODEL"))

        return cls(
            bot_token=bot_token,
            ai_api_key=ai_api_key,
            ai_base_url=get("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            ai_model=ai_models[0],
            ai_models=ai_models,
            ai_timeout_seconds=_parse_float(src.get("AI_TIMEOUT_SECONDS"), 25.0, minimum=3.0),
            fallback_ai_api_key=get("FALLBACK_AI_API_KEY"),
            fallback_ai_base_url=get("FALLBACK_AI_BASE_URL").rstrip("/"),
            fallback_ai_model=fallback_ai_models[0] if fallback_ai_models else "",
            fallback_ai_models=fallback_ai_models,
            vision_model=get("VISION_MODEL"),
            image_gen_model=get("IMAGE_GEN_MODEL"),
            tavily_api_key=get("TAVILY_API_KEY"),
            cf_account_id=get("CF_ACCOUNT_ID"),
            cf_api_token=get("CF_API_TOKEN"),
            cf_llm_model=get("CF_LLM_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
            ocr_api_key=get("OCR_API_KEY"),
            supabase_url=get("SUPABASE_URL").rstrip("/"),
            supabase_key=get("SUPABASE_KEY"),
            admin_users=_parse_id_set(src.get("ADMIN_USERS")),
            allow_dms=_parse_bool(src.get("ALLOW_DMS"), True),
            allow_groups=_parse_bool(src.get("ALLOW_GROUPS"), True),
            allowed_groups=_parse_id_set(src.get("ALLOWED_GROUPS")),
            blocked_users=_parse_id_set(src.get("BLOCKED_USERS")),
            blocked_groups=_parse_id_set(src.get("BLOCKED_GROUPS")),
            group_trigger_mode=group_trigger_mode,
            rate_limit_burst=_parse_int(src.get("RATE_LIMIT_BURST"), 3, minimum=0),
            rate_limit_burst_window=_parse_int(src.get("RATE_LIMIT_BURST_WINDOW"), 10, minimum=1),
            rate_limit_per_minute=_parse_int(src.get("RATE_LIMIT_PER_MINUTE"), 6, minimum=0),
            group_rate_limit_per_minute=_parse_int(src.get("GROUP_RATE_LIMIT_PER_MINUTE"), 20, minimum=0),
            daily_msg_limit=_parse_int(src.get("DAILY_MSG_LIMIT"), 50, minimum=0),
            daily_search_limit=_parse_int(src.get("DAILY_SEARCH_LIMIT"), 15, minimum=0),
            daily_image_limit=_parse_int(src.get("DAILY_IMAGE_LIMIT"), 5, minimum=0),
            daily_file_limit=_parse_int(src.get("DAILY_FILE_LIMIT"), 10, minimum=0),
            admin_bypass_limits=_parse_bool(src.get("ADMIN_BYPASS_LIMITS"), True),
            max_input_chars=_parse_int(src.get("MAX_INPUT_CHARS"), 4000, minimum=200, maximum=32000),
            max_file_bytes=_parse_int(src.get("MAX_FILE_BYTES"), 10_000_000, minimum=100_000, maximum=20_000_000),
            max_url_bytes=_parse_int(src.get("MAX_URL_BYTES"), 500_000, minimum=50_000, maximum=5_000_000),
            max_history_messages=max_hist,
            history_retention_days=_parse_int(src.get("HISTORY_RETENTION_DAYS"), 30, minimum=0),
            enable_web_search=_parse_bool(src.get("ENABLE_WEB_SEARCH"), True),
            enable_url_reader=_parse_bool(src.get("ENABLE_URL_READER"), True),
            enable_jina_reader=_parse_bool(src.get("ENABLE_JINA_READER"), True),
            enable_vision=_parse_bool(src.get("ENABLE_VISION"), True),
            enable_ocr_space=_parse_bool(src.get("ENABLE_OCR_SPACE"), True),
            enable_file_gen=_parse_bool(src.get("ENABLE_FILE_GEN"), True),
            enable_image_gen=_parse_bool(src.get("ENABLE_IMAGE_GEN"), True),
            enable_pollinations_fallback=_parse_bool(src.get("ENABLE_POLLINATIONS_FALLBACK"), True),
            enable_charts=_parse_bool(src.get("ENABLE_CHARTS"), True),
            enable_dm_profile_memory=_parse_bool(src.get("ENABLE_DM_PROFILE_MEMORY"), True),
            data_dir=data_dir,
            db_path=db_path,
            allowed_web_ports=_parse_ports(src.get("ALLOWED_WEB_PORTS"), (80, 443)),
            webhook_url=webhook_url,
            webhook_path=webhook_path,
            webhook_secret=get("WEBHOOK_SECRET"),
            blocked_ssrf_hosts=blocked_hosts,
            port=port,
            log_level=log_level,
        )
