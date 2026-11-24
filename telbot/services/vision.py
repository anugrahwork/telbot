"""Image vision and OCR service supporting OpenAI-compatible vision, Cloudflare AI, and OCR.space."""

from __future__ import annotations

import asyncio
import base64
import logging

import httpx
from openai import AsyncOpenAI

from telbot.config import Settings

logger = logging.getLogger(__name__)


def detect_image_mime(raw_bytes: bytes) -> str:
    """Detect common image MIME types via magic bytes."""
    if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP":
        return "image/webp"
    if raw_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return "image/jpeg"


class VisionService:
    """Combines native OpenAI-compatible Vision with optional Cloudflare Vision & OCR.space."""

    def __init__(self, settings: Settings, primary_client: AsyncOpenAI) -> None:
        self.settings = settings
        self.primary_client = primary_client
        self._disabled_cf_models: set[str] = set()
        self._llama_vision_agreed = False

    @property
    def has_any_engine(self) -> bool:
        if not self.settings.enable_vision:
            return False
        return bool(
            self.settings.vision_model
            or self.settings.has_cloudflare
            or (self.settings.enable_ocr_space and self.settings.ocr_api_key)
        )

    async def analyze_image(self, raw_bytes: bytes, user_prompt: str = "") -> str:
        """Analyze an image using configured vision/OCR backends and return consensus text."""
        if not self.settings.enable_vision:
            return ""

        mime = detect_image_mime(raw_bytes)
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"

        tasks: list[tuple[str, object]] = []
        if self.settings.vision_model:
            tasks.append(("Primary Vision Model", self._openai_vision(data_url, user_prompt)))
        if self.settings.has_cloudflare:
            tasks.append(("Cloudflare High-Res Vision", self._cf_highres_vision(raw_bytes, data_url, user_prompt)))
            tasks.append(("Cloudflare Secondary Vision", self._cf_llava_vision(raw_bytes, user_prompt)))
        if self.settings.enable_ocr_space and self.settings.ocr_api_key:
            tasks.append(("OCR Engine 2 (Math/Numbers)", self._ocr_space(data_url, "2")))
            tasks.append(("OCR Engine 1 (Print Check)", self._ocr_space(data_url, "1")))

        if not tasks:
            return ""

        results = await asyncio.gather(*(t[1] for t in tasks))
        parts = [f"[{label}]\n{text}" for (label, _), text in zip(tasks, results) if text]
        if not parts:
            return ""

        if len(parts) > 1:
            parts.append(
                "Compare all transcriptions above carefully. If digits or fractions differ between engines "
                "(e.g. 3/4 vs 3/5), explain both interpretations clearly."
            )
        return "\n\n".join(parts)

    async def _openai_vision(self, data_url: str, user_prompt: str) -> str:
        prompt = (
            "Transcribe every visible number, fraction, math symbol, and text carefully, "
            f"then describe the image clearly. User question: {user_prompt}"
        )
        try:
            resp = await self.primary_client.chat.completions.create(
                model=self.settings.vision_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                max_tokens=800,
            )
            choices = getattr(resp, "choices", None) or []
            content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
            return str(content).strip() if content else ""
        except Exception as exc:
            logger.warning("OpenAI-compatible vision (%s) failed: %s", self.settings.vision_model, exc)
            return ""

    async def _cf_highres_vision(self, raw_bytes: bytes, data_url: str, user_prompt: str) -> str:
        headers = {"Authorization": f"Bearer {self.settings.cf_api_token}"}
        v_prompt = (
            "Transcribe every visible number, fraction, math symbol, and word character-by-character, "
            f"then describe the image. User question: {user_prompt}"
        )
        candidates = [
            (
                "@cf/google/gemma-3-12b-it",
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": v_prompt},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        }
                    ],
                    "max_tokens": 700,
                },
            ),
            (
                "@cf/meta/llama-3.2-11b-vision-instruct",
                {"prompt": v_prompt, "image": list(raw_bytes), "max_tokens": 700},
            ),
        ]
        for model_id, payload in candidates:
            if model_id in self._disabled_cf_models:
                continue
            url = f"https://api.cloudflare.com/client/v4/accounts/{self.settings.cf_account_id}/ai/run/{model_id}"
            try:
                async with httpx.AsyncClient(timeout=25.0) as client:
                    if "llama-3.2" in model_id and not self._llama_vision_agreed:
                        await client.post(url, headers=headers, json={"prompt": "agree"})
                        self._llama_vision_agreed = True
                    resp = await client.post(url, headers=headers, json=payload)
                    if resp.status_code >= 400:
                        if resp.status_code in (401, 403, 404):
                            self._disabled_cf_models.add(model_id)
                        continue
                    result = resp.json().get("result", {}) or {}
                    content = str(result.get("response") or result.get("description") or "").strip()
                    if content:
                        return content
            except Exception as exc:
                logger.warning("Cloudflare vision (%s) failed: %s", model_id, exc)
        return ""

    async def _cf_llava_vision(self, raw_bytes: bytes, user_prompt: str) -> str:
        v_prompt = f"Describe this image and transcribe all visible text/math accurately. Question: {user_prompt}"
        payload = {"prompt": v_prompt, "image": list(raw_bytes), "max_tokens": 512}
        for model_id in ("@cf/llava-hf/llava-1.5-7b-hf", "@cf/unum/uform-gen2-qwen-500m"):
            if model_id in self._disabled_cf_models:
                continue
            url = f"https://api.cloudflare.com/client/v4/accounts/{self.settings.cf_account_id}/ai/run/{model_id}"
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {self.settings.cf_api_token}"},
                        json=payload,
                    )
                    if resp.status_code in (401, 403, 404):
                        self._disabled_cf_models.add(model_id)
                        continue
                    resp.raise_for_status()
                    desc = str(resp.json().get("result", {}).get("description", "")).strip()
                    if desc:
                        return desc
            except Exception as exc:
                logger.warning("Cloudflare secondary vision (%s) failed: %s", model_id, exc)
        return ""

    async def _ocr_space(self, data_url: str, engine: str) -> str:
        data = {
            "base64Image": data_url,
            "apikey": self.settings.ocr_api_key,
            "OCREngine": engine,
            "scale": "true",
            "isTable": "true",
        }
        if engine == "2":
            data["detectOrientation"] = "true"
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.post("https://api.ocr.space/parse/image", data=data)
                resp.raise_for_status()
                parsed = resp.json().get("ParsedResults") or []
                if parsed and isinstance(parsed, list):
                    return str(parsed[0].get("ParsedText", "")).strip()
        except Exception as exc:
            logger.warning("OCR.space engine %s failed: %s", engine, exc)
        return ""
