"""Tornado webhook server with constant-time secret validation and /health endpoint."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import signal

import tornado.web
from telegram import Update
from telegram.ext import Application

from telbot.config import Settings

logger = logging.getLogger(__name__)


def build_webhook_tornado_app(app: Application, settings: Settings) -> tornado.web.Application:
    """Create the Tornado web application serving /health, /, and the Telegram webhook route."""
    webhook_secret = settings.effective_webhook_secret
    webhook_path = settings.webhook_path or "/webhook"

    class HealthHandler(tornado.web.RequestHandler):
        def get(self) -> None:
            self.write("OK")

        def head(self) -> None:
            self.set_status(200)

    class WebhookHandler(tornado.web.RequestHandler):
        async def post(self) -> None:
            incoming = self.request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(incoming, webhook_secret):
                raise tornado.web.HTTPError(403)
            try:
                data = json.loads(self.request.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise tornado.web.HTTPError(400) from exc
            if not isinstance(data, dict):
                raise tornado.web.HTTPError(400)
            update = Update.de_json(data, app.bot)
            if update:
                await app.update_queue.put(update)
            self.write("OK")

    return tornado.web.Application(
        [
            (r"/", HealthHandler),
            (r"/health", HealthHandler),
            (re.escape(webhook_path), WebhookHandler),
        ]
    )


async def run_webhook_server(
    app: Application,
    settings: Settings,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Start Tornado webhook + /health HTTP server with explicit post_init and post_shutdown hooks."""
    webhook_secret = settings.effective_webhook_secret
    stop_evt = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_evt.set)
        except (NotImplementedError, RuntimeError):
            pass

    async with app:
        if app.post_init is not None:
            await app.post_init(app)

        await app.start()
        web_app = build_webhook_tornado_app(app, settings)
        server = None
        try:
            server = web_app.listen(settings.port, address="0.0.0.0")
            logger.info("Webhook + /health server listening on 0.0.0.0:%s", settings.port)
            await app.bot.set_webhook(
                url=settings.webhook_url,
                secret_token=webhook_secret,
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
            )
            logger.info("Telegram webhook registered at %s", settings.webhook_url)
            await stop_evt.wait()
        finally:
            if server is not None:
                server.stop()
            if getattr(app, "running", True):
                await app.stop()
            if app.post_shutdown is not None:
                await app.post_shutdown(app)
