"""Vendor-neutral OpenAI-compatible multi-provider LLM router with failover & cooldowns."""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI

from telbot.config import Settings

logger = logging.getLogger(__name__)

RATE_LIMIT_COOLDOWN_SECONDS = 60.0
SERVER_COOLDOWN_SECONDS = 300.0
CREDITS_COOLDOWN_SECONDS = 3600.0


class LLMRouter:
    """Routes chat completions across configured primary, fallback, and optional Cloudflare providers."""

    def __init__(self, settings: Settings, *, retry_delay_seconds: float = 2.0) -> None:
        self.settings = settings
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        timeout = httpx.Timeout(settings.ai_timeout_seconds, connect=6.0)

        self.primary_client = AsyncOpenAI(
            api_key=settings.ai_api_key or "unset",
            base_url=settings.ai_base_url,
            max_retries=0,
            timeout=timeout,
        )

        self.fallback_client: AsyncOpenAI | None = None
        if settings.fallback_ai_api_key:
            self.fallback_client = AsyncOpenAI(
                api_key=settings.fallback_ai_api_key,
                base_url=settings.fallback_ai_base_url or settings.ai_base_url,
                max_retries=0,
                timeout=timeout,
            )

        self.retired_models: set[tuple[str, str]] = set()
        self.provider_cooldown_until: dict[str, float] = {}
        self.model_cooldown_until: dict[tuple[str, str], float] = {}

    async def aclose(self) -> None:
        for client in (self.primary_client, self.fallback_client):
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass

    def provider_tag(self, prov_key: str) -> str:
        if prov_key == "cloudflare":
            return "cloudflare"
        url = self.settings.ai_base_url if prov_key == "primary" else (self.settings.fallback_ai_base_url or self.settings.ai_base_url)
        host = (urlparse(url).hostname or "").lower()
        return host or prov_key

    def is_model_available(self, prov_key: str, model_id: str) -> bool:
        return bool(model_id) and (prov_key, model_id) not in self.retired_models

    def _client_for(self, prov_key: str) -> AsyncOpenAI | None:
        if prov_key == "primary":
            return self.primary_client
        if prov_key == "fallback":
            return self.fallback_client
        return None

    async def _call_cloudflare(self, messages: list[dict[str, str]], model_id: str, max_tokens: int) -> tuple[str, int]:
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.settings.cf_account_id}/ai/run/{model_id}"
        async with httpx.AsyncClient(timeout=self.settings.ai_timeout_seconds) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {self.settings.cf_api_token}"},
                json={"messages": messages, "max_tokens": max_tokens},
            )
            resp.raise_for_status()
            result = resp.json().get("result", {}) or {}
            content = str(result.get("response") or "").strip()
            if not content:
                raise RuntimeError("Empty response from Cloudflare Workers AI")
            tokens = max(1, (sum(len(m["content"]) for m in messages) + len(content)) // 4)
            return content, tokens

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        err_str = str(exc)
        return "429" in err_str or "rate limit" in err_str.lower()

    async def _invoke_candidate(
        self,
        client: AsyncOpenAI | None,
        model_id: str,
        prov_key: str,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> tuple[str, int]:
        if prov_key == "cloudflare":
            return await self._call_cloudflare(messages, model_id, max_tokens)
        assert client is not None
        response = await client.chat.completions.create(
            model=model_id,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
            temperature=0.7,
        )
        choices = getattr(response, "choices", None) or []
        content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
        if content and str(content).strip():
            usage = getattr(response, "usage", None)
            tokens = int(getattr(usage, "total_tokens", 0) or 0)
            if not tokens:
                tokens = max(1, (sum(len(m["content"]) for m in messages) + len(str(content))) // 4)
            return str(content).strip(), tokens
        raise RuntimeError(f"Empty completion from {prov_key}:{model_id}")

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        preferred_model: tuple[str, str] | None = None,
        max_tokens: int = 2000,
    ) -> tuple[str, int]:
        """Generate a completion trying configured providers and models in priority order."""
        candidates: list[tuple[AsyncOpenAI | None, str, str]] = []
        seen: set[tuple[str, str]] = set()

        def _add(prov_key: str, model_id: str) -> None:
            if not model_id or (prov_key, model_id) in seen or not self.is_model_available(prov_key, model_id):
                return
            if prov_key == "cloudflare":
                if not self.settings.has_cloudflare:
                    return
                seen.add((prov_key, model_id))
                candidates.append((None, model_id, prov_key))
                return
            client = self._client_for(prov_key)
            if client is None:
                return
            seen.add((prov_key, model_id))
            candidates.append((client, model_id, prov_key))

        if preferred_model:
            _add(preferred_model[0], preferred_model[1])

        for model_id in self.settings.ai_models or (self.settings.ai_model,):
            _add("primary", model_id)
        if self.fallback_client is not None:
            for model_id in self.settings.fallback_ai_models or (self.settings.ai_model,):
                _add("fallback", model_id)
        if self.settings.has_cloudflare:
            _add("cloudflare", self.settings.cf_llm_model)

        if not candidates:
            # All candidates retired in-session; allow primary configured model as last resort
            candidates.append((self.primary_client, self.settings.ai_model, "primary"))

        if len(candidates) > 1:
            now_mono = time.monotonic()
            candidates.sort(
                key=lambda item: (
                    1
                    if (
                        self.provider_cooldown_until.get(item[2], 0.0) > now_mono
                        or self.model_cooldown_until.get((item[2], item[1]), 0.0) > now_mono
                    )
                    else 0
                )
            )

        last_error: Exception | None = None
        saw_rate_limit = False
        for client, model_id, prov_key in candidates:
            try:
                return await self._invoke_candidate(client, model_id, prov_key, messages, max_tokens)
            except Exception as exc:
                logger.warning("Provider %s (%s) failed: %s", prov_key, model_id, exc)
                self._record_failure(prov_key, model_id, exc)
                if self._is_rate_limit_error(exc):
                    saw_rate_limit = True
                last_error = exc

        if saw_rate_limit:
            retry_candidates = [c for c in candidates if self.is_model_available(c[2], c[1])]
            if retry_candidates:
                if self.retry_delay_seconds > 0:
                    await asyncio.sleep(self.retry_delay_seconds)
                client, model_id, prov_key = retry_candidates[0]
                try:
                    return await self._invoke_candidate(client, model_id, prov_key, messages, max_tokens)
                except Exception as exc:
                    logger.warning("Retry on %s (%s) failed: %s", prov_key, model_id, exc)
                    self._record_failure(prov_key, model_id, exc)
                    last_error = exc

        raise RuntimeError(f"All AI providers failed: {last_error}")

    def _record_failure(self, prov_key: str, model_id: str, exc: Exception) -> None:
        err_str = str(exc)
        err_lower = err_str.lower()
        no_credits = any(
            marker in err_lower
            for marker in ("insufficient_credits", "insufficient_quota", "no available platform credits", "add credits")
        )
        locked_or_missing = no_credits or any(
            marker in err_lower or marker in err_str
            for marker in ("model_requires_purchase", "locked on your account", "402", "403", "404", "not found")
        )
        if locked_or_missing:
            self.retired_models.add((prov_key, model_id))
            if no_credits:
                self.provider_cooldown_until[prov_key] = time.monotonic() + CREDITS_COOLDOWN_SECONDS
            return
        if self._is_rate_limit_error(exc):
            self.model_cooldown_until[(prov_key, model_id)] = time.monotonic() + RATE_LIMIT_COOLDOWN_SECONDS
            return
        if any(marker in err_lower or marker in err_str for marker in ("timed out", "timeout", "502", "503", "504", "connect")):
            self.provider_cooldown_until[prov_key] = time.monotonic() + SERVER_COOLDOWN_SECONDS

    async def list_available_models(self) -> list[tuple[str, str, str]]:
        """Discover models from configured OpenAI-compatible endpoints plus Cloudflare backup."""
        choices: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()

        def _append(prov_key: str, model_id: str) -> None:
            if model_id and (prov_key, model_id) not in seen and self.is_model_available(prov_key, model_id):
                seen.add((prov_key, model_id))
                choices.append((prov_key, model_id, self.provider_tag(prov_key)))

        for model_id in self.settings.ai_models or (self.settings.ai_model,):
            _append("primary", model_id)
        if self.fallback_client is not None:
            for model_id in self.settings.fallback_ai_models:
                _append("fallback", model_id)

        async def _discover(client: AsyncOpenAI, prov_key: str, limit: int) -> None:
            added = 0
            try:
                page = await asyncio.wait_for(client.models.list(), timeout=4.0)
                for m in getattr(page, "data", []):
                    mid = getattr(m, "id", "")
                    if mid and (prov_key, mid) not in seen and self.is_model_available(prov_key, mid):
                        _append(prov_key, mid)
                        added += 1
                        if added >= limit or len(choices) >= 14:
                            break
            except Exception as exc:
                logger.debug("Could not list models for %s: %s", prov_key, exc)

        tasks = [_discover(self.primary_client, "primary", 6)]
        if self.fallback_client is not None:
            tasks.append(_discover(self.fallback_client, "fallback", 4))
        await asyncio.gather(*tasks)

        if self.settings.has_cloudflare:
            _append("cloudflare", self.settings.cf_llm_model)

        return choices[:14]

    def health_summary(self) -> str:
        now = time.monotonic()
        paused = [
            f"{self.provider_tag(k)} ({int(until - now)}s cooldown)"
            for k, until in self.provider_cooldown_until.items()
            if until > now
        ]
        paused.extend(
            f"{self.provider_tag(k)}/{m} ({int(until - now)}s cooldown)"
            for (k, m), until in self.model_cooldown_until.items()
            if until > now
        )
        return ", ".join(paused) if paused else "All configured providers healthy"
