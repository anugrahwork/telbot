"""Telegram formatting helpers and Smart Hybrid Table rendering."""

from telbot.formatting.tables import (
    flatten_markdown_tables,
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

__all__ = [
    "flatten_markdown_tables",
    "format_compact_table_pre",
    "is_compact_table",
    "parse_md_tables",
    "render_table_png_bytes",
    "chunk_telegram_text",
    "deduplicate_reply_paragraphs",
    "to_telegram_html",
]
