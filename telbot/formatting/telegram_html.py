"""Markdown-to-Telegram-HTML conversion, deduplication, and chunking."""

from __future__ import annotations

import html
import re

from telbot.formatting.tables import (
    flatten_markdown_tables,
    format_compact_table_pre,
    is_compact_table,
    parse_md_tables,
)

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://(?:[^\s()]|\([^\s()]*\))+)\)")


def to_telegram_html(text: str) -> str:
    """Convert Markdown (including compact aligned tables and safe links) into Telegram HTML."""
    placeholders: dict[str, str] = {}

    def _stash(rendered: str) -> str:
        token = f"\x00CODEBLOCK{len(placeholders)}\x00"
        placeholders[token] = rendered
        return token

    parts: list[str] = []
    for kind, payload in parse_md_tables(text):
        if kind == "table":
            headers, rows, raw_block = payload  # type: ignore[misc]
            if is_compact_table(headers, rows):
                parts.append(_stash(format_compact_table_pre(headers, rows)))
            else:
                parts.append(html.escape(flatten_markdown_tables(str(raw_block)), quote=False))
        else:
            seg = html.escape(str(payload), quote=False)
            seg = re.sub(
                r"```(?:[\w+-]+)?\n?(.*?)```",
                lambda m: _stash(f"<pre>{m.group(1)}</pre>"),
                seg,
                flags=re.DOTALL,
            )
            parts.append(seg)

    esc = "\n".join(parts)
    esc = re.sub(r"`([^`\n]+)`", lambda m: _stash(f"<code>{m.group(1)}</code>"), esc)

    def _link_sub(m: re.Match[str]) -> str:
        label = m.group(1)
        safe_url = m.group(2).replace('"', "%22")
        return _stash(f'<a href="{safe_url}">{label}</a>')

    esc = _MD_LINK_RE.sub(_link_sub, esc)
    esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
    esc = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", esc, flags=re.MULTILINE)

    for token, rendered in placeholders.items():
        esc = esc.replace(token, rendered)
    return esc


def _split_blocks_preserving_fences(text: str) -> list[str]:
    blocks: list[str] = []
    cur: list[str] = []
    in_fence = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and not line.strip():
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def deduplicate_reply_paragraphs(text: str) -> str:
    """Drop restated or near-duplicate prose paragraphs while preserving code blocks and tables."""
    blocks = _split_blocks_preserving_fences(text)
    if len(blocks) <= 1:
        return text

    kept: list[str] = []
    seen_exact: set[str] = set()
    seen_token_sets: list[set[str]] = []

    for blk in blocks:
        stripped = blk.strip()
        if not stripped:
            continue
        is_code = stripped.startswith("```")
        is_table = any(kind == "table" for kind, _ in parse_md_tables(stripped))
        if is_code or is_table:
            kept.append(blk)
            continue

        norm = re.sub(r"\s+", " ", stripped.lower())
        if len(norm) >= 15 and norm in seen_exact:
            continue

        tokens = set(re.findall(r"[a-z0-9]{3,}", norm))
        is_dup = False
        if len(tokens) >= 6:
            for prev_tokens in seen_token_sets:
                union = tokens | prev_tokens
                if union and (len(tokens & prev_tokens) / len(union)) >= 0.72:
                    is_dup = True
                    break
        if is_dup:
            continue

        kept.append(blk)
        if len(norm) >= 15:
            seen_exact.add(norm)
        if len(tokens) >= 6:
            seen_token_sets.append(tokens)

    return "\n\n".join(kept)


def chunk_telegram_text(text: str, limit: int = 3900) -> list[str]:
    """Split oversized replies cleanly along paragraph or line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = para if not current else f"{current}\n\n{para}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(para) <= limit:
            current = para
            continue
        line_buf = ""
        for line in para.split("\n"):
            cand_line = line if not line_buf else f"{line_buf}\n{line}"
            if len(cand_line) <= limit:
                line_buf = cand_line
            else:
                if line_buf:
                    chunks.append(line_buf)
                while len(line) > limit:
                    chunks.append(line[:limit])
                    line = line[limit:]
                line_buf = line
        current = line_buf
    if current:
        chunks.append(current)
    return chunks
