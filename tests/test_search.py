"""Tests for paywall-skipping search filtering, DDG HTML parsing, and citation stripping."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from telbot.config import Settings
from telbot.services.search import (
    SearchService,
    _TextExtractor,
    clean_url_token,
    filter_search_results,
    has_paywall_text,
    is_paywalled_or_login_url,
    parse_duckduckgo_results,
    strip_bad_citations,
)


class SearchTests(unittest.TestCase):
    def test_paywall_url_and_text_detection(self) -> None:
        self.assertTrue(is_paywalled_or_login_url("https://www.nytimes.com/2026/01/01/world/news.html"))
        self.assertTrue(is_paywalled_or_login_url("https://example.com/auth/login"))
        self.assertTrue(is_paywalled_or_login_url("https://en.wikipedia.org/wiki/Special:UserLogin"))
        self.assertFalse(is_paywalled_or_login_url("https://en.wikipedia.org/wiki/Python_(programming_language)"))
        self.assertFalse(is_paywalled_or_login_url("https://docs.python.org/3/library/sqlite3.html"))
        self.assertTrue(has_paywall_text("Please subscribe to continue reading this article."))

    def test_filter_search_results_drops_paywalls_and_boosts_trusted_open_sources(self) -> None:
        raw = [
            {
                "title": "Python asyncio guide",
                "url": "https://medium.com/some-author/python-asyncio-guide",
                "content": "Member-only story on python asyncio guide",
                "score": 0.99,
            },
            {
                "title": "Python asyncio blog",
                "url": "https://randomblog.example.com/asyncio",
                "content": "Tutorial for python asyncio basics",
                "score": 0.70,
            },
            {
                "title": "asyncio — Asynchronous I/O — Python documentation",
                "url": "https://docs.python.org/3/library/asyncio.html",
                "content": "Official python asyncio guide and reference documentation",
                "score": 0.65,
            },
        ]
        filtered = filter_search_results("python asyncio guide", raw)
        urls = [r["url"] for r in filtered]
        self.assertNotIn("https://medium.com/some-author/python-asyncio-guide", urls)
        self.assertEqual(urls[0], "https://docs.python.org/3/library/asyncio.html")

    def test_parse_duckduckgo_results_extracts_real_urls_and_snippets(self) -> None:
        sample_html = """
        <div class="result">
          <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&amp;rut=abc">Python Docs</a>
          <a class="result__snippet" href="#">Official Python 3 documentation and tutorials.</a>
        </div>
        <div class="result">
          <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.wsj.com%2Farticles%2Ftest">WSJ Paywall</a>
          <a class="result__snippet" href="#">Subscribe to read more.</a>
        </div>
        """
        parsed = parse_duckduckgo_results(sample_html)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["url"], "https://docs.python.org/3/")
        self.assertEqual(parsed[0]["title"], "Python Docs")
        self.assertIn("Official Python 3", parsed[0]["content"])

    def test_parse_duckduckgo_results_handles_reversed_attribute_order(self) -> None:
        reversed_html = """
        <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2F" class="result__a">Stdlib</a>
        <span class="result__snippet">Python Standard Library docs.</span>
        """
        parsed = parse_duckduckgo_results(reversed_html)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["url"], "https://docs.python.org/3/library/")
        self.assertEqual(parsed[0]["title"], "Stdlib")
        self.assertIn("Standard Library", parsed[0]["content"])

    def test_strip_bad_citations_cleans_broken_links_without_breaking_prose(self) -> None:
        reply = (
            "Read the [Official Docs](https://docs.python.org/3/) and [Python Wiki](https://en.wikipedia.org/wiki/Python_(programming_language)) "
            "and see [Dead Link](https://dead.example.com/404).\n"
            "- https://paywalled.wsj.com/article\n"
            "More details (https://broken.example.com/500) are available."
        )
        cleaned = strip_bad_citations(
            reply,
            {"https://dead.example.com/404", "https://paywalled.wsj.com/article", "https://broken.example.com/500"},
        )
        self.assertIn("[Official Docs](https://docs.python.org/3/)", cleaned)
        self.assertIn("[Python Wiki](https://en.wikipedia.org/wiki/Python_(programming_language))", cleaned)
        self.assertIn("Dead Link", cleaned)
        self.assertNotIn("https://dead.example.com/404", cleaned)
        self.assertNotIn("https://paywalled.wsj.com/article", cleaned)
        self.assertNotIn("https://broken.example.com/500", cleaned)

    def test_clean_url_token_and_html_scraper_nested_skip_tags(self) -> None:
        self.assertEqual(
            clean_url_token("https://en.wikipedia.org/wiki/Python_(programming_language))."),
            "https://en.wikipedia.org/wiki/Python_(programming_language)",
        )
        raw_html = """
        <html>
          <head>
            <meta name="description" content="Concise page summary here." />
            <style>.hidden { display: none; }</style>
          </head>
          <body>
            <noscript><style>body{}</style>Enable JS warning</noscript>
            <h1>Main Title</h1>
            <p>First real paragraph.</p>
            <script>var secret = "ignore me";</script>
            <p>Second real paragraph.</p>
          </body>
        </html>
        """
        parser = _TextExtractor()
        parser.feed(raw_html)
        scraped = parser.get_text()
        self.assertIn("Concise page summary here.", scraped)
        self.assertIn("Main Title\nFirst real paragraph.\nSecond real paragraph.", scraped)
        self.assertNotIn("Enable JS warning", scraped)
        self.assertNotIn("ignore me", scraped)
        self.assertNotIn("display: none", scraped)


class FetchPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_403_falls_through_to_jina_reader(self) -> None:
        settings = Settings.from_env({"BOT_TOKEN": "123:abc", "AI_API_KEY": "sk-test"})
        service = SearchService(settings)

        class _FakeStreamResp:
            def __init__(self, url: str, status_code: int, body: bytes) -> None:
                self.url = url
                self.status_code = status_code
                self.headers = {"content-type": "text/plain; charset=utf-8"}
                self._body = body

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def raise_for_status(self) -> None:
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            async def aiter_bytes(self):
                yield self._body

        class _FakeClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, method: str, target_url: str, **kwargs):
                if target_url.startswith("https://r.jina.ai/"):
                    return _FakeStreamResp(
                        target_url,
                        200,
                        b"# Scraped Article\n\nThis is clean accessible markdown content retrieved via Jina Reader.",
                    )
                return _FakeStreamResp(target_url, 403, b"Forbidden")

        async def _always_safe(url: str) -> bool:
            return True

        with patch.object(service, "_is_safe_url", side_effect=_always_safe):
            with patch("telbot.services.search.httpx.AsyncClient", _FakeClient):
                preview = await service.fetch_url_preview("https://example.com/article")

        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertIn("Scraped Article", preview)
