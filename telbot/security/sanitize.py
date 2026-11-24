"""Prompt-injection sanitization, per-request nonced wrappers, filename & secret guards."""

from __future__ import annotations

import os
import re
import secrets

_TAG_ESCAPE_RE = re.compile(r"</?\s*untrusted_data(?:_[a-z0-9]+)?\s*>", re.IGNORECASE)
_SAFE_NAME_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")

ALLOWED_OUTGOING_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".docx",
        ".md",
        ".txt",
        ".py",
        ".js",
        ".ts",
        ".json",
        ".csv",
        ".html",
        ".css",
        ".sql",
        ".xml",
        ".svg",
        ".yml",
        ".yaml",
    }
)


def sanitize_untrusted_text(raw: str) -> str:
    """Strip NUL bytes and neutralize any untrusted_data tag-breakout attempts."""
    if not raw:
        return ""
    no_nul = raw.replace("\x00", "")
    cleaned = _TAG_ESCAPE_RE.sub("[redacted_tag]", no_nul)
    return cleaned.strip()


def wrap_untrusted(raw: str, label: str = "") -> tuple[str, str]:
    """Wrap external text in a unique per-request nonced delimiter tag.

    Returns ``(wrapped_text, tag_name)``.
    """
    nonce = secrets.token_hex(4)
    tag = f"untrusted_data_{nonce}"
    cleaned = sanitize_untrusted_text(raw)
    clean_label = sanitize_untrusted_text(label).replace("\n", " ")[:120]
    header = f"[Source: {clean_label}]\n" if clean_label else ""
    return f"<{tag}>\n{header}{cleaned}\n</{tag}>", tag


def sanitize_filename(raw_name: str, default: str = "output.txt") -> str:
    """Sanitize a model-supplied filename against path traversal and unsafe extensions."""
    base = (raw_name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = _SAFE_NAME_CHARS_RE.sub("_", base).strip("._")
    if not base:
        return default

    stem, ext = os.path.splitext(base)
    ext_lower = ext.lower()
    if not stem:
        stem = "output"
    if ext_lower not in ALLOWED_OUTGOING_EXTENSIONS:
        ext_lower = ".txt"

    stem = stem[:64]
    return f"{stem}{ext_lower}"


def mask_secret(value: str, visible: int = 4) -> str:
    """Mask sensitive tokens/keys for safe diagnostic display."""
    if not value:
        return "(unset)"
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"
