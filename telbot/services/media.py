"""Optional AI image generation (OpenAI Images / Cloudflare FLUX / Pollinations) and QuickChart charts."""

from __future__ import annotations

import base64
import json
import logging
from urllib.parse import quote_plus

import httpx
from openai import AsyncOpenAI

from telbot.config import Settings
from telbot.security.ssrf import is_safe_public_url_async, make_redirect_guard

logger = logging.getLogger(__name__)


def build_chart_config(spec: str) -> dict | None:
    """Parse Chart.js JSON or 'type=bar | title=... | labels=A,B | values=1,2' into a Chart.js dict."""
    s = spec.strip()
    if not s:
        return None
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict) and "data" in obj:
                return obj
        except Exception:
            return None

    parts: dict[str, str] = {}
    for seg in s.split("|"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            parts[k.strip().lower()] = v.strip()

    chart_type = parts.get("type", "bar").lower()
    if chart_type not in {"bar", "line", "pie", "doughnut"}:
        chart_type = "bar"
    labels = [lbl.strip() for lbl in parts.get("labels", "").split(",") if lbl.strip()]
    values_raw = [v.strip() for v in parts.get("values", "").split(",") if v.strip()]
    values: list[float] = []
    for v in values_raw:
        try:
            values.append(float(v))
        except ValueError:
            values.append(0.0)
    if not labels or not values:
        return None

    title = parts.get("title", "")
    palette = ["#1E3A5F", "#2563EB", "#0EA5E9", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6"]
    colors = [palette[i % len(palette)] for i in range(len(values))] if chart_type in {"pie", "doughnut", "bar"} else "#2563EB"
    cfg: dict = {
        "type": chart_type,
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": parts.get("series", title or "Value"),
                    "data": values,
                    "backgroundColor": colors,
                    "borderColor": "#1E3A5F" if chart_type == "line" else colors,
                    "fill": chart_type != "line",
                }
            ],
        },
    }
    if title:
        cfg["options"] = {"plugins": {"title": {"display": True, "text": title}}}
    return cfg


class MediaService:
    """Handles AI image generation and QuickChart PNG chart rendering."""

    def __init__(self, settings: Settings, primary_client: AsyncOpenAI) -> None:
        self.settings = settings
        self.primary_client = primary_client
        self._redirect_hook = make_redirect_guard(
            blocked_hosts=settings.blocked_ssrf_hosts,
            allowed_ports=settings.allowed_web_ports,
        )

    async def generate_image(self, prompt: str) -> bytes | None:
        if not self.settings.enable_image_gen or not prompt.strip():
            return None

        clean_prompt = prompt.strip()[:800]

        if self.settings.image_gen_model:
            try:
                resp = await self.primary_client.images.generate(
                    model=self.settings.image_gen_model,
                    prompt=clean_prompt,
                    size="1024x1024",
                    response_format="b64_json",
                )
                data = getattr(resp, "data", None) or []
                if data:
                    b64_val = getattr(data[0], "b64_json", None)
                    if b64_val:
                        return base64.b64decode(b64_val)
                    url_val = getattr(data[0], "url", None)
                    if url_val and await is_safe_public_url_async(
                        url_val,
                        blocked_hosts=self.settings.blocked_ssrf_hosts,
                        allowed_ports=self.settings.allowed_web_ports,
                    ):
                        async with httpx.AsyncClient(
                            follow_redirects=True,
                            max_redirects=5,
                            timeout=25.0,
                            event_hooks={"response": [self._redirect_hook]},
                        ) as client:
                            dl = await client.get(url_val)
                            dl.raise_for_status()
                            if dl.content and len(dl.content) > 1000:
                                return dl.content
            except Exception as exc:
                logger.warning("OpenAI-compatible image generation failed: %s", exc)

        if self.settings.has_cloudflare:
            cf_url = (
                f"https://api.cloudflare.com/client/v4/accounts/{self.settings.cf_account_id}"
                "/ai/run/@cf/black-forest-labs/flux-1-schnell"
            )
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        cf_url,
                        headers={"Authorization": f"Bearer {self.settings.cf_api_token}"},
                        json={"prompt": clean_prompt, "steps": 6},
                    )
                    resp.raise_for_status()
                    b64_img = resp.json().get("result", {}).get("image")
                    if b64_img:
                        return base64.b64decode(b64_img)
            except Exception as exc:
                logger.warning("Cloudflare image generation failed: %s", exc)

        if self.settings.enable_pollinations_fallback:
            img_url = (
                f"https://image.pollinations.ai/prompt/{quote_plus(clean_prompt)}"
                "?width=1024&height=1024&model=flux&enhance=true&nologo=true"
            )
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    max_redirects=5,
                    timeout=25.0,
                    event_hooks={"response": [self._redirect_hook]},
                ) as client:
                    resp = await client.get(img_url)
                    resp.raise_for_status()
                    if resp.content and len(resp.content) > 1000:
                        return resp.content
            except Exception as exc:
                logger.warning("Pollinations image generation failed: %s", exc)

        return None

    async def fetch_chart_image(self, spec: str) -> bytes | None:
        if not self.settings.enable_charts:
            return None
        cfg = build_chart_config(spec)
        if not cfg:
            return None
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.post(
                    "https://quickchart.io/chart",
                    json={
                        "width": 720,
                        "height": 380,
                        "backgroundColor": "white",
                        "format": "png",
                        "chart": cfg,
                    },
                )
                resp.raise_for_status()
                if resp.content and len(resp.content) > 500:
                    return resp.content
        except Exception as exc:
            logger.warning("QuickChart rendering failed: %s", exc)
        return None
