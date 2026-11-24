"""Tests for PDF/DOCX builders, Tornado webhook routes/auth/lifecycle, and container healthcheck."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from telbot.config import Settings
from telbot.formatting.tables import render_table_png_bytes
from telbot.handlers.webhook import build_webhook_tornado_app, run_webhook_server
from telbot.healthcheck import run_healthcheck
from telbot.services.documents import extract_docx_text, extract_pdf_smart
from telbot.services.file_builder import build_docx_bytes, build_pdf_bytes


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FileBuilderTests(unittest.TestCase):
    def test_pdf_and_docx_build_and_extract_roundtrip(self) -> None:
        md = (
            "# Community Guide\n\n"
            "## Overview\n\n"
            "Check **bold text** and [Python Docs](https://docs.python.org/3/).\n\n"
            "| Feature | Status |\n"
            "| :--- | :--- |\n"
            "| Tables | Ready |\n\n"
            "__IMG__\n\n"
            "- Point one\n"
        )
        png = render_table_png_bytes(["Feature", "Status", "Notes", "Mode"], [["Tables", "Ready", "Hybrid", "Auto"]])
        assert png is not None
        pdf_bytes = build_pdf_bytes(md, {"__IMG__": (png, "Sample chart")})
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        extracted_pdf = extract_pdf_smart(pdf_bytes)
        self.assertIn("Community Guide", extracted_pdf)

        docx_bytes = build_docx_bytes(md)
        extracted_docx = extract_docx_text(docx_bytes)
        self.assertIn("Community Guide", extracted_docx)
        self.assertIn("Python Docs", extracted_docx)


class _DummyApp:
    def __init__(self, post_init=None, post_shutdown=None) -> None:
        self.post_init = post_init
        self.post_shutdown = post_shutdown
        self.running = False
        self.update_queue: asyncio.Queue = asyncio.Queue()
        self.bot = SimpleNamespace(
            set_webhook=AsyncMock(),
        )
        self.events: list[str] = []

    async def __aenter__(self):
        self.events.append("aenter")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.events.append("aexit")
        return False

    async def start(self) -> None:
        self.running = True
        self.events.append("start")

    async def stop(self) -> None:
        self.running = False
        self.events.append("stop")


class WebhookAndHealthcheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_tornado_health_and_webhook_secret_validation(self) -> None:
        port = _free_port()
        settings = Settings.from_env(
            {
                "BOT_TOKEN": "123:abc",
                "AI_API_KEY": "sk-test",
                "WEBHOOK_URL": f"https://example.com/webhook",
                "WEBHOOK_SECRET": "super-secret-token",
                "PORT": str(port),
            }
        )
        app = _DummyApp()
        web_app = build_webhook_tornado_app(app, settings)  # type: ignore[arg-type]
        server = web_app.listen(port, address="127.0.0.1")
        try:
            async with httpx.AsyncClient() as client:
                r_health = await client.get(f"http://127.0.0.1:{port}/health")
                self.assertEqual(r_health.status_code, 200)
                self.assertEqual(r_health.text, "OK")

                r_forbidden = await client.post(
                    f"http://127.0.0.1:{port}/webhook",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
                    json={"update_id": 1},
                )
                self.assertEqual(r_forbidden.status_code, 403)

                r_bad_json = await client.post(
                    f"http://127.0.0.1:{port}/webhook",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "super-secret-token"},
                    content=b"{not-json",
                )
                self.assertEqual(r_bad_json.status_code, 400)

                r_ok = await client.post(
                    f"http://127.0.0.1:{port}/webhook",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "super-secret-token"},
                    content=json.dumps({"update_id": 42}),
                )
                self.assertEqual(r_ok.status_code, 200)
                self.assertFalse(app.update_queue.empty())

                # Healthcheck in webhook mode passes while Tornado is listening (run off-thread so Tornado can reply)
                hc_rc = await asyncio.to_thread(
                    run_healthcheck,
                    {"WEBHOOK_URL": "https://example.com/webhook", "PORT": str(port)},
                )
                self.assertEqual(hc_rc, 0)
        finally:
            server.stop()

        # Healthcheck fails cleanly once the webhook server is stopped
        self.assertEqual(
            run_healthcheck({"WEBHOOK_URL": "https://example.com/webhook", "PORT": str(port)}),
            1,
        )

    async def test_run_webhook_server_invokes_post_init_and_post_shutdown(self) -> None:
        port = _free_port()
        settings = Settings.from_env(
            {
                "BOT_TOKEN": "123:abc",
                "AI_API_KEY": "sk-test",
                "WEBHOOK_URL": "https://example.com/webhook",
                "WEBHOOK_SECRET": "secret-123",
                "PORT": str(port),
            }
        )
        lifecycle: list[str] = []

        async def _post_init(_app):
            lifecycle.append("post_init")

        async def _post_shutdown(_app):
            lifecycle.append("post_shutdown")

        app = _DummyApp(post_init=_post_init, post_shutdown=_post_shutdown)
        stop_evt = asyncio.Event()
        stop_evt.set()

        await run_webhook_server(app, settings, stop_event=stop_evt)  # type: ignore[arg-type]

        self.assertEqual(lifecycle, ["post_init", "post_shutdown"])
        self.assertEqual(app.events, ["aenter", "start", "stop", "aexit"])
        app.bot.set_webhook.assert_awaited_once()

    def test_healthcheck_polling_mode(self) -> None:
        self.assertEqual(run_healthcheck({}, container_check=False), 0)
        with patch("telbot.healthcheck.os.path.exists", return_value=False):
            self.assertEqual(run_healthcheck({}), 0)
        with tempfile.TemporaryDirectory() as tmp:
            cmdline_py = os.path.join(tmp, "cmdline_py")
            with open(cmdline_py, "wb") as f:
                f.write(b"python\x00bot.py\x00")
            self.assertEqual(
                run_healthcheck({}, container_check=True, proc_cmdline_path=cmdline_py),
                0,
            )

            cmdline_init = os.path.join(tmp, "cmdline_init")
            with open(cmdline_init, "wb") as f:
                f.write(b"/sbin/init\x00")
            self.assertEqual(
                run_healthcheck({}, container_check=True, proc_cmdline_path=cmdline_init),
                1,
            )
