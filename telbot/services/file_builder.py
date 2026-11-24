"""Styled downloadable PDF (fpdf2 + fallback) and OOXML DOCX builders."""

from __future__ import annotations

import html
import io
import logging
import re
import textwrap
import zipfile
from xml.sax.saxutils import escape as xml_escape

try:
    from fpdf import FPDF  # type: ignore
    from fpdf.fonts import FontFace  # type: ignore
except ImportError:  # pragma: no cover
    FPDF = None  # type: ignore[assignment]
    FontFace = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_LATIN_REPLACEMENTS = str.maketrans(
    {
        "\u2013": "-",
        "\u2014": "--",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2022": "-",
        "\u2026": "...",
        "\u00a0": " ",
    }
)


def _to_pdf_latin(text: str) -> str:
    return text.translate(_LATIN_REPLACEMENTS).encode("latin-1", errors="replace").decode("latin-1")


def _inline_md_to_pdf_html(line: str) -> str:
    esc = html.escape(_to_pdf_latin(line), quote=False)
    placeholders: dict[str, str] = {}

    def _stash(val: str) -> str:
        key = f"\x00L{len(placeholders)}\x00"
        placeholders[key] = val
        return key

    def _md_link(m: re.Match) -> str:
        lbl = m.group(1)
        url = m.group(2).replace('"', "%22")
        return _stash(f'<a href="{url}"><u><font color="#1D4ED8">{lbl}</font></u></a>')

    esc = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", _md_link, esc)

    def _bare_link(m: re.Match) -> str:
        raw = m.group(0).rstrip(".,;:)]>\"'")
        trail = m.group(0)[len(raw) :]
        url = raw.replace('"', "%22")
        return _stash(f'<a href="{url}"><u><font color="#1D4ED8">{raw}</font></u></a>') + trail

    esc = re.sub(r"https?://[^\s<>\"']+", _bare_link, esc)
    esc = re.sub(r"`([^`\n]+)`", r'<font face="Courier" size="9">\1</font>', esc)
    esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
    for k, v in placeholders.items():
        esc = esc.replace(k, v)
    return esc


def _build_plain_pdf_fallback(text: str) -> bytes:
    safe_ascii = _to_pdf_latin(text)
    wrapped_lines: list[str] = []
    for raw_line in safe_ascii.splitlines() or [""]:
        wrapped_lines.extend(textwrap.wrap(raw_line, width=82) or [""] if raw_line.strip() else [""])
    pages = [wrapped_lines[i : i + 52] for i in range(0, max(1, len(wrapped_lines)), 52)]
    objects: list[bytes] = []
    kids = " ".join(f"{4 + i * 2} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for idx, page_lines in enumerate(pages):
        content_id = 5 + idx * 2
        ops = ["BT", "/F1 11 Tf", "14 TL", "50 760 Td"]
        for ln in page_lines:
            esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({esc}) Tj T*")
        ops.append("ET")
        stream_b = "\n".join(ops).encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode("ascii")
        )
        objects.append(f"<< /Length {len(stream_b)} >>\nstream\n".encode("ascii") + stream_b + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii"))
    return bytes(out)


def build_pdf_bytes(text: str, media_map: dict[str, tuple[bytes, str]] | None = None) -> bytes:
    """Generate a styled multi-page PDF with headings, bold, tables, links, and optional embedded images/charts."""
    if FPDF is None:
        return _build_plain_pdf_fallback(text)

    class _StyledPDF(FPDF):
        def footer(self) -> None:
            self.set_y(-13)
            self.set_font("Helvetica", "I", 8.5)
            self.set_text_color(120, 125, 135)
            self.cell(0, 8, f"Page {self.page_no()} / {{nb}}", align="C")

    pdf = _StyledPDF(format="A4")
    pdf.alias_nb_pages()
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_font("Helvetica", size=10.5)
    pdf.set_text_color(30, 35, 45)

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()

        if media_map and s in media_map:
            img_bytes, caption = media_map[s]
            try:
                if pdf.get_y() > 205:
                    pdf.add_page()
                pdf.ln(3)
                pdf.image(io.BytesIO(img_bytes), x=27, w=156)
                if caption:
                    pdf.ln(2)
                    pdf.set_font("Helvetica", "I", 8.5)
                    pdf.set_text_color(95, 100, 110)
                    pdf.multi_cell(0, 4.5, _to_pdf_latin(caption), align="C")
                    pdf.set_text_color(30, 35, 45)
                pdf.ln(4)
            except Exception as exc:
                logger.warning("Could not embed media in PDF: %s", exc)
            i += 1
            continue

        if s.startswith("```"):
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(_to_pdf_latin(lines[i]))
                i += 1
            i += 1
            if code_lines:
                pdf.ln(2)
                pdf.set_fill_color(243, 244, 246)
                pdf.set_font("Courier", size=8.5)
                pdf.multi_cell(0, 4.5, "\n".join(code_lines), fill=True)
                pdf.set_font("Helvetica", size=10.5)
                pdf.ln(2)
            continue

        if s.startswith("|") and s.endswith("|") and s.count("|") >= 2:
            table_rows: list[list[str]] = []
            while i < len(lines):
                ts = lines[i].strip()
                if not (ts.startswith("|") and ts.endswith("|") and ts.count("|") >= 2):
                    break
                cells = [c.strip() for c in ts.strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    table_rows.append([_to_pdf_latin(re.sub(r"\*\*(.+?)\*\*", r"\1", c)) for c in cells])
                i += 1
            if table_rows:
                col_count = max(len(r) for r in table_rows)
                pdf.ln(2)
                pdf.set_font("Helvetica", size=9.5)
                h_style = FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=(30, 58, 95))
                with pdf.table(
                    headings_style=h_style,
                    cell_fill_color=(244, 247, 250),
                    cell_fill_mode="ROWS",
                    line_height=5.5,
                    padding=2,
                ) as tbl:
                    for r in table_rows:
                        padded = r + [""] * (col_count - len(r))
                        row_obj = tbl.row()
                        for cell_txt in padded:
                            m_lnk = re.match(r"^\[([^\]]+)\]\((https?://[^\s)]+)\)$", cell_txt.strip())
                            if m_lnk:
                                row_obj.cell(m_lnk.group(1), link=m_lnk.group(2))
                            elif re.fullmatch(r"https?://\S+", cell_txt.strip()):
                                row_obj.cell(cell_txt.strip(), link=cell_txt.strip())
                            else:
                                row_obj.cell(cell_txt)
                pdf.set_font("Helvetica", size=10.5)
                pdf.ln(3)
            continue

        if s in {"---", "***", "___"}:
            y = pdf.get_y() + 2
            pdf.set_draw_color(200, 205, 215)
            pdf.line(18, y, 192, y)
            pdf.ln(5)
            i += 1
            continue

        if not s:
            pdf.ln(3)
            i += 1
            continue

        if s.startswith("# "):
            pdf.ln(3)
            pdf.set_font("Helvetica", "B", 16)
            pdf.set_text_color(30, 58, 95)
            pdf.multi_cell(0, 7.5, _to_pdf_latin(re.sub(r"\*\*(.+?)\*\*", r"\1", s[2:].strip())))
            y = pdf.get_y() + 1
            pdf.set_draw_color(30, 58, 95)
            pdf.line(18, y, 192, y)
            pdf.ln(4)
            pdf.set_font("Helvetica", size=10.5)
            pdf.set_text_color(30, 35, 45)
            i += 1
            continue

        if s.startswith("## "):
            pdf.ln(2.5)
            pdf.set_font("Helvetica", "B", 13)
            pdf.set_text_color(43, 76, 126)
            pdf.multi_cell(0, 6.5, _to_pdf_latin(re.sub(r"\*\*(.+?)\*\*", r"\1", s[3:].strip())))
            pdf.ln(1.5)
            pdf.set_font("Helvetica", size=10.5)
            pdf.set_text_color(30, 35, 45)
            i += 1
            continue

        if s.startswith("### "):
            pdf.ln(1.5)
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_text_color(50, 55, 65)
            pdf.multi_cell(0, 6, _to_pdf_latin(re.sub(r"\*\*(.+?)\*\*", r"\1", s[4:].strip())))
            pdf.ln(1)
            pdf.set_font("Helvetica", size=10.5)
            pdf.set_text_color(30, 35, 45)
            i += 1
            continue

        m_num = re.match(r"^(\d+\.)\s+(.+)$", s)
        if s.startswith("- ") or s.startswith("* ") or m_num:
            bullet_prefix = m_num.group(1) if m_num else "-"
            body_txt = m_num.group(2) if m_num else s[2:].strip()
            pdf.set_x(22)
            try:
                pdf.set_font("Helvetica", size=10.5)
                pdf.set_text_color(30, 35, 45)
                pdf.write_html(f"<b>{bullet_prefix}</b> {_inline_md_to_pdf_html(body_txt)}")
                pdf.ln(5.5)
            except Exception:
                pdf.set_font("Helvetica", size=10.5)
                pdf.set_text_color(30, 35, 45)
                pdf.multi_cell(0, 5.5, _to_pdf_latin(f"{bullet_prefix} {body_txt}"))
            finally:
                pdf.set_font("Helvetica", size=10.5)
                pdf.set_text_color(30, 35, 45)
                pdf.set_x(pdf.l_margin)
            i += 1
            continue

        try:
            pdf.set_font("Helvetica", size=10.5)
            pdf.set_text_color(30, 35, 45)
            pdf.write_html(_inline_md_to_pdf_html(s))
            pdf.ln(6)
        except Exception:
            pdf.set_font("Helvetica", size=10.5)
            pdf.set_text_color(30, 35, 45)
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(0, 5.5, _to_pdf_latin(s))
        finally:
            pdf.set_font("Helvetica", size=10.5)
            pdf.set_text_color(30, 35, 45)
            pdf.set_x(pdf.l_margin)
        i += 1

    return bytes(pdf.output())


def build_docx_bytes(text: str) -> bytes:
    """Generate a styled Microsoft Word (.docx) file with headings, bold text, and HTTP(S) hyperlinks."""
    link_rels: list[tuple[str, str]] = []

    def _runs_xml(line_txt: str, font_half_pts: int | None = None, force_bold: bool = False) -> str:
        tokens = re.split(r"(\[[^\]]+\]\(https?://[^\s)]+\)|https?://[^\s<>\"']+|\*\*[^*]+\*\*)", line_txt)
        parts: list[str] = []
        rpr_base = ""
        if font_half_pts:
            rpr_base += f'<w:sz w:val="{font_half_pts}"/><w:szCs w:val="{font_half_pts}"/>'
        if force_bold:
            rpr_base += "<w:b/>"

        for tok in tokens:
            if not tok:
                continue
            m_md = re.fullmatch(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", tok)
            if m_md or re.fullmatch(r"https?://[^\s<>\"']+", tok):
                lbl = m_md.group(1) if m_md else tok.rstrip(".,;:)]>\"'")
                url = m_md.group(2) if m_md else lbl
                trail = "" if m_md else tok[len(lbl) :]
                rid = f"rIdLink{len(link_rels) + 1}"
                link_rels.append((rid, url))
                parts.append(
                    f'<w:hyperlink r:id="{rid}"><w:r><w:rPr><w:color w:val="1D4ED8"/><w:u w:val="single"/></w:rPr>'
                    f'<w:t xml:space="preserve">{xml_escape(lbl)}</w:t></w:r></w:hyperlink>'
                )
                if trail:
                    parts.append(f'<w:r><w:t xml:space="preserve">{xml_escape(trail)}</w:t></w:r>')
            elif tok.startswith("**") and tok.endswith("**") and len(tok) > 4:
                inner = tok[2:-2]
                parts.append(
                    f'<w:r><w:rPr><w:b/>{rpr_base}</w:rPr><w:t xml:space="preserve">{xml_escape(inner)}</w:t></w:r>'
                )
            else:
                rpr = f"<w:rPr>{rpr_base}</w:rPr>" if rpr_base else ""
                parts.append(f'<w:r>{rpr}<w:t xml:space="preserve">{xml_escape(tok)}</w:t></w:r>')
        return "".join(parts)

    paragraphs: list[str] = []
    for raw_line in text.splitlines() or [""]:
        s = raw_line.strip()
        if s.startswith("# "):
            paragraphs.append(f"<w:p>{_runs_xml(s[2:].strip(), font_half_pts=34, force_bold=True)}</w:p>")
        elif s.startswith("## "):
            paragraphs.append(f"<w:p>{_runs_xml(s[3:].strip(), font_half_pts=28, force_bold=True)}</w:p>")
        elif s.startswith("### "):
            paragraphs.append(f"<w:p>{_runs_xml(s[4:].strip(), font_half_pts=24, force_bold=True)}</w:p>")
        else:
            paragraphs.append(f"<w:p>{_runs_xml(raw_line)}</w:p>")

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<w:body>{''.join(paragraphs)}</w:body></w:document>"
    )
    doc_rels_items = "".join(
        f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
        f'Target="{xml_escape(url)}" TargetMode="External"/>'
        for rid, url in link_rels
    )
    doc_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{doc_rels_items}</Relationships>"
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/_rels/document.xml.rels", doc_rels_xml)
    return bio.getvalue()
