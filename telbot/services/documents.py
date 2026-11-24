"""Safe document text extraction for PDF, zip-bomb/XXE-guarded DOCX, and text/code files."""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None  # type: ignore[assignment]

MAX_PDF_PAGES = 50
MAX_DOC_CHARS = 25_000
MAX_DOCX_XML_BYTES = 10_000_000

TEXT_FILE_EXTENSIONS = frozenset(
    {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".log", ".html", ".css", ".yml", ".yaml", ".sql", ".xml", ".svg"}
)
IMAGE_FILE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def extract_pdf_smart(
    raw_bytes: bytes,
    query_hint: str = "",
    *,
    max_pages: int = MAX_PDF_PAGES,
    max_chars: int = MAX_DOC_CHARS,
) -> str:
    """Extract text from up to max_pages of a PDF (scoring pages by query relevance when larger)."""
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed.")
    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
    except Exception as exc:
        raise ValueError("Invalid or corrupted PDF file.") from exc
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ValueError("Encrypted PDF cannot be read without a password.") from exc

    total_pages = len(reader.pages)
    if total_pages <= max_pages:
        pages_text = [
            f"[Page {i + 1}]\n{page.extract_text() or ''}"
            for i, page in enumerate(reader.pages)
        ]
        joined = "\n\n".join(pages_text).strip()
        return joined[:max_chars]

    keywords = [w.lower() for w in re.findall(r"\w{3,}", query_hint)]
    scored_pages: list[tuple[int, int, str]] = []
    for idx, page in enumerate(reader.pages):
        txt = page.extract_text() or ""
        if not txt.strip():
            continue
        score = sum(txt.lower().count(k) for k in keywords) if keywords else 0
        if idx < 5:
            score += 1
        scored_pages.append((score, idx, txt))

    scored_pages.sort(key=lambda item: (-item[0], item[1]))
    selected = sorted(scored_pages[:max_pages], key=lambda item: item[1])
    pages_text = [f"[Page {idx + 1} of {total_pages}]\n{txt}" for _, idx, txt in selected]
    return "\n\n".join(pages_text).strip()[:max_chars]


def extract_docx_text(
    raw_bytes: bytes,
    *,
    max_chars: int = MAX_DOC_CHARS,
    max_xml_bytes: int = MAX_DOCX_XML_BYTES,
) -> str:
    """Extract plain text from a .docx Word file with zip-bomb and XXE/DOCTYPE protections."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            try:
                info = zf.getinfo("word/document.xml")
            except KeyError as exc:
                raise ValueError("Invalid DOCX: missing word/document.xml") from exc

            if info.file_size > max_xml_bytes:
                raise ValueError("DOCX XML exceeds safe uncompressed size limit")

            with zf.open(info) as member:
                xml_bytes = member.read(max_xml_bytes + 1)
            if len(xml_bytes) > max_xml_bytes:
                raise ValueError("DOCX XML stream exceeds safe size limit")
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid or corrupted DOCX file.") from exc

    head_upper = xml_bytes[:4096].upper()
    if b"<!DOCTYPE" in head_upper or b"<!ENTITY" in head_upper:
        raise ValueError("Unsafe XML DOCTYPE/ENTITY declaration in DOCX")

    try:
        tree = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError("Invalid or corrupted DOCX XML.") from exc
    paragraphs: list[str] = []
    for elem in tree.iter():
        if elem.tag.endswith("}p"):
            texts = [node.text for node in elem.iter() if node.tag.endswith("}t") and node.text]
            if texts:
                paragraphs.append("".join(texts))
    return "\n".join(paragraphs).strip()[:max_chars]


def extract_text_file(raw_bytes: bytes, *, max_chars: int = MAX_DOC_CHARS) -> str:
    """Decode UTF-8 text/code files safely."""
    return raw_bytes.decode("utf-8", errors="replace").strip()[:max_chars]
