"""Tests for Smart Hybrid Tables, Pillow PNG rendering, HTML escaping, and chunking."""

from __future__ import annotations

import io
import unittest

from PIL import Image

from telbot.formatting.tables import (
    format_compact_table_pre,
    is_compact_table,
    parse_md_tables,
    render_table_png_bytes,
)
from telbot.formatting.telegram_html import (
    chunk_telegram_text,
    deduplicate_reply_paragraphs,
    to_telegram_html,
)


class TablesAndHTMLTests(unittest.TestCase):
    def test_compact_table_renders_aligned_monospace_html(self) -> None:
        md = (
            "Here is the summary:\n\n"
            "| Plan | Price |\n"
            "| :--- | ---: |\n"
            "| Free | $0 |\n"
            "| Pro  | $19 |\n"
        )
        segs = parse_md_tables(md)
        tables = [p for k, p in segs if k == "table"]
        self.assertEqual(len(tables), 1)
        headers, rows, _ = tables[0]  # type: ignore[misc]
        self.assertTrue(is_compact_table(headers, rows))

        pre = format_compact_table_pre(headers, rows)
        self.assertTrue(pre.startswith("<pre>") and pre.endswith("</pre>"))
        self.assertIn("│", pre)
        rendered = to_telegram_html(md)
        self.assertIn("<pre>", rendered)

    def test_wide_table_renders_valid_png_bytes(self) -> None:
        headers = ["Provider", "Model", "Context Window", "Supports Vision", "Notes"]
        rows = [
            ["OpenAI", "gpt-4o-mini", "128,000 tokens", "Yes", "Fast general assistant"],
            ["Groq", "llama-3.3-70b", "128,000 tokens", "No", "Low-latency inference"],
        ]
        self.assertFalse(is_compact_table(headers, rows))
        png_bytes = render_table_png_bytes(headers, rows)
        self.assertIsNotNone(png_bytes)
        assert png_bytes is not None
        self.assertTrue(png_bytes.startswith(b"\x89PNG"))
        img = Image.open(io.BytesIO(png_bytes))
        self.assertGreaterEqual(img.width, 760)
        self.assertGreater(img.height, 100)

    def test_ignores_tables_inside_fenced_code_blocks(self) -> None:
        md = "Example:\n```markdown\n| A | B |\n|---|---|\n| 1 | 2 |\n```"
        segs = parse_md_tables(md)
        self.assertFalse(any(k == "table" for k, _ in segs))

    def test_telegram_html_blocks_unsafe_tags_and_js_links(self) -> None:
        raw = (
            'Hello <script>alert(1)</script> **bold** `x < y` '
            '[Docs](https://example.com) '
            '[Wiki](https://en.wikipedia.org/wiki/Python_(programming_language)) '
            '[Bad](javascript:alert(1))'
        )
        out = to_telegram_html(raw)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("<b>bold</b>", out)
        self.assertIn("<code>x &lt; y</code>", out)
        self.assertIn('<a href="https://example.com">Docs</a>', out)
        self.assertIn('<a href="https://en.wikipedia.org/wiki/Python_(programming_language)">Wiki</a>', out)
        self.assertNotIn('href="javascript:', out)

    def test_deduplicate_and_chunk(self) -> None:
        dup_text = (
            "Python is a high-level programming language used widely across web backends and automation.\n\n"
            "Python is a high-level programming language used widely across web backends and automation!"
        )
        deduped = deduplicate_reply_paragraphs(dup_text)
        self.assertEqual(len(deduped.split("\n\n")), 1)

        long_text = ("Paragraph one with enough words.\n\n" * 40).strip()
        chunks = chunk_telegram_text(long_text, limit=300)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 300 for c in chunks))
