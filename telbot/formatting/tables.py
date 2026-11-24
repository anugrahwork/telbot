"""Smart Hybrid Tables: compact Unicode monospace <pre> blocks or high-clarity Pillow PNG images."""

from __future__ import annotations

import html
import io
import logging
import math
import re

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_NUM_CELL_RE = re.compile(r"^[$€£¥+-]?\d[\d,.]*(?:%|x|ms|s|k|M|B)?$")


def flatten_markdown_tables(text: str) -> str:
    """Convert markdown table rows into readable 'a — b — c' lines as a plain-text fallback."""
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and s.count("|") >= 2:
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            out.append(" — ".join(c for c in cells if c))
        else:
            out.append(line)
    return "\n".join(out)


def _split_md_table_row(line: str) -> list[str]:
    core = line.strip()
    if core.startswith("|"):
        core = core[1:]
    if core.endswith("|") and not core.endswith(r"\|"):
        core = core[:-1]
    parts = re.split(r"(?<!\\)\|", core)
    return [p.replace(r"\|", "|").strip() for p in parts]


def _is_md_table_separator(cells: list[str]) -> bool:
    return len(cells) >= 2 and all(bool(re.fullmatch(r":?-{2,}:?", c)) for c in cells)


def _clean_table_cell(text: str, *, allow_newlines: bool = False) -> str:
    cleaned = re.sub(r"<br\s*/?>", "\n" if allow_newlines else " ", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1", cleaned)
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.+?)__", r"\1", cleaned)
    cleaned = re.sub(r"(?<!\*)\*([^\s*][^*\n]*?[^\s*]|[^\s*])\*(?!\*)", r"\1", cleaned)
    cleaned = re.sub(r"`([^`\n]+)`", r"\1", cleaned)
    if allow_newlines:
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in cleaned.split("\n")]
        return "\n".join(ln for ln in lines if ln).strip()
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_md_tables(text: str) -> list[tuple[str, object]]:
    """Split text into ordered ('text', str) and ('table', (headers, rows, raw_block)) segments."""
    lines = text.replace("\r\n", "\n").split("\n")
    segments: list[tuple[str, object]] = []
    text_buf: list[str] = []
    in_fence = False
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            text_buf.append(lines[i])
            i += 1
            continue

        if not in_fence and i + 1 < len(lines) and "|" in stripped and "|" in lines[i + 1]:
            headers = _split_md_table_row(lines[i])
            sep_cells = _split_md_table_row(lines[i + 1])
            if len(headers) >= 2 and len(sep_cells) == len(headers) and _is_md_table_separator(sep_cells):
                col_count = len(headers)
                body_rows: list[list[str]] = []
                j = i + 2
                while j < len(lines):
                    row_line = lines[j]
                    row_stripped = row_line.strip()
                    if not row_stripped or row_stripped.startswith("```") or "|" not in row_stripped:
                        break
                    cells = _split_md_table_row(row_line)
                    if _is_md_table_separator(cells):
                        j += 1
                        continue
                    if len(cells) < col_count:
                        cells.extend([""] * (col_count - len(cells)))
                    elif len(cells) > col_count:
                        cells = cells[: col_count - 1] + [" | ".join(cells[col_count - 1 :])]
                    body_rows.append(cells)
                    j += 1

                if body_rows:
                    if text_buf:
                        segments.append(("text", "\n".join(text_buf)))
                        text_buf = []
                    segments.append(("table", (headers, body_rows, "\n".join(lines[i:j]))))
                    i = j
                    continue

        text_buf.append(lines[i])
        i += 1

    if text_buf:
        segments.append(("text", "\n".join(text_buf)))
    return segments


def is_compact_table(headers: list[str], rows: list[list[str]]) -> bool:
    """Return True if a table fits cleanly inside a mobile monospace <pre> block."""
    if len(headers) > 3 or len(rows) > 15 or not rows:
        return False
    for cell in [*headers, *(c for row in rows for c in row)]:
        if "\n" in _clean_table_cell(cell, allow_newlines=True):
            return False
    clean_h = [_clean_table_cell(h) for h in headers]
    clean_r = [[_clean_table_cell(c) for c in row] for row in rows]
    col_widths = [max(len(clean_h[c]), *(len(r[c]) for r in clean_r)) for c in range(len(clean_h))]
    total_width = sum(col_widths) + 3 * (len(col_widths) - 1)
    return total_width <= 40


def format_compact_table_pre(headers: list[str], rows: list[list[str]]) -> str:
    """Render a compact table as an aligned Unicode monospace <pre> block for Telegram HTML."""
    clean_h = [_clean_table_cell(h) for h in headers]
    clean_r = [[_clean_table_cell(c) for c in row] for row in rows]
    ncols = len(clean_h)
    widths = [max(len(clean_h[c]), *(len(r[c]) for r in clean_r)) for c in range(ncols)]
    right_align = [
        all(bool(_NUM_CELL_RE.match(r[c])) for r in clean_r if r[c]) and any(r[c] for r in clean_r)
        for c in range(ncols)
    ]

    def _fmt_row(cells: list[str], allow_num_align: bool = True) -> str:
        formatted = []
        for idx, val in enumerate(cells):
            if allow_num_align and right_align[idx]:
                formatted.append(val.rjust(widths[idx]))
            else:
                formatted.append(val.ljust(widths[idx]))
        return " │ ".join(formatted).rstrip()

    divider = "─┼─".join("─" * widths[c] for c in range(ncols))
    grid_lines = [_fmt_row(clean_h, allow_num_align=False), divider]
    grid_lines.extend(_fmt_row(r) for r in clean_r)
    return f"<pre>{html.escape(chr(10).join(grid_lines), quote=False)}</pre>"


def _load_table_font(size: int, bold: bool = False):
    if ImageFont is None:
        return None
    candidates = (
        [
            "segoeuib.ttf",
            "arialbd.ttf",
            "DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "segoeui.ttf",
            "arial.ttf",
            "DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "LiberationSans-Regular.ttf",
        ]
    )
    for font_path in candidates:
        try:
            return ImageFont.truetype(font_path, size=size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _font_line_height(font, fallback_size: int) -> int:
    try:
        asc, desc = font.getmetrics()
        return asc + desc + 6
    except Exception:
        return fallback_size + 8


def _wrap_table_text_px(draw, text: str, font, max_w: int) -> list[str]:
    if not text:
        return [""]
    max_w = max(40, max_w)
    wrapped: list[str] = []
    for para in text.split("\n"):
        words = para.split(" ")
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if draw.textlength(candidate, font=font) <= max_w:
                current = candidate
                continue
            if current:
                wrapped.append(current)
                current = ""
            if draw.textlength(word, font=font) <= max_w:
                current = word
            else:
                chunk = ""
                for ch in word:
                    if draw.textlength(chunk + ch, font=font) <= max_w:
                        chunk += ch
                    else:
                        if chunk:
                            wrapped.append(chunk)
                        chunk = ch
                current = chunk
        wrapped.append(current)
    return wrapped or [""]


def render_table_png_bytes(headers: list[str], rows: list[list[str]]) -> bytes | None:
    """Render a wide Markdown table as a styled high-clarity PNG image."""
    if Image is None or ImageDraw is None or ImageFont is None:
        return None
    try:
        clean_h = [_clean_table_cell(h, allow_newlines=True) for h in headers]
        clean_r = [[_clean_table_cell(c, allow_newlines=True) for c in row] for row in rows]
        ncols = len(clean_h)
        if ncols < 2 or not clean_r:
            return None

        font_h = _load_table_font(22, bold=True)
        font_b = _load_table_font(20, bold=False)
        if font_h is None or font_b is None:
            return None

        probe_img = Image.new("RGB", (10, 10), "white")
        probe_draw = ImageDraw.Draw(probe_img)

        pad_x = 18
        pad_y = 13
        outer_pad = 24
        min_canvas_w = 760
        max_canvas_w = 1400
        max_height_px = 4500
        max_rows = 60

        def _max_line_px(s: str, font) -> float:
            lines = s.split("\n") if s else [""]
            return max(probe_draw.textlength(ln, font=font) for ln in lines)

        header_px = [_max_line_px(clean_h[c], font_h) + 2 * pad_x for c in range(ncols)]
        body_px = [
            max((_max_line_px(r[c], font_b) + 2 * pad_x for r in clean_r[:max_rows]), default=0.0)
            for c in range(ncols)
        ]

        min_col_w = [max(105, min(260, int(math.ceil(header_px[c])) + 6)) for c in range(ncols)]
        nat_col_w = [
            max(min_col_w[c], min(440, int(math.ceil(max(header_px[c], body_px[c]))) + 8))
            for c in range(ncols)
        ]

        min_table_w = min_canvas_w - 2 * outer_pad
        max_table_w = max_canvas_w - 2 * outer_pad
        total_nat = sum(nat_col_w)
        table_w = max(min_table_w, min(max_table_w, total_nat))

        if sum(min_col_w) >= table_w:
            scale = table_w / max(1, sum(min_col_w))
            col_widths = [max(72, int(w * scale)) for w in min_col_w]
        elif total_nat <= table_w:
            col_widths = list(nat_col_w)
            slack = table_w - total_nat
            weights = [max(1, w) for w in nat_col_w]
            w_sum = sum(weights)
            for c in range(ncols):
                col_widths[c] += int(slack * weights[c] / w_sum)
        else:
            col_widths = list(min_col_w)
            avail = table_w - sum(min_col_w)
            flex = [max(0, nat_col_w[c] - min_col_w[c]) for c in range(ncols)]
            flex_sum = max(1, sum(flex))
            for c in range(ncols):
                col_widths[c] += int(avail * flex[c] / flex_sum)

        col_widths[-1] += table_w - sum(col_widths)

        line_h_h = _font_line_height(font_h, 22)
        line_h_b = _font_line_height(font_b, 20)

        wrapped_h = [
            _wrap_table_text_px(probe_draw, clean_h[c], font_h, col_widths[c] - 2 * pad_x)
            for c in range(ncols)
        ]
        header_row_h = 2 * pad_y + max(len(w) for w in wrapped_h) * line_h_h - 4

        notice_text = ""
        rendered_rows: list[tuple[list[list[str]], int]] = []
        used_h = outer_pad * 2 + header_row_h
        notice_reserve = 56
        total_input_rows = len(clean_r)
        candidate_rows = clean_r[:max_rows]

        for idx, row in enumerate(candidate_rows):
            wrapped_row = [
                _wrap_table_text_px(probe_draw, row[c], font_b, col_widths[c] - 2 * pad_x)
                for c in range(ncols)
            ]
            row_h = 2 * pad_y + max(len(w) for w in wrapped_row) * line_h_b - 4
            remaining_after = (idx + 1) < total_input_rows
            cap_limit = max_height_px - (notice_reserve if remaining_after else 0)
            if used_h + row_h > cap_limit and rendered_rows:
                hidden = total_input_rows - len(rendered_rows)
                notice_text = f"... +{hidden} more rows omitted (ask for a CSV or PDF export for all {total_input_rows} rows)"
                break
            rendered_rows.append((wrapped_row, row_h))
            used_h += row_h

        if not notice_text and total_input_rows > len(rendered_rows):
            hidden = total_input_rows - len(rendered_rows)
            notice_text = f"... +{hidden} more rows omitted (ask for a CSV or PDF export for all {total_input_rows} rows)"

        notice_h = 2 * pad_y + line_h_b - 4 if notice_text else 0
        canvas_w = table_w + 2 * outer_pad
        canvas_h = used_h + notice_h

        img = Image.new("RGB", (canvas_w, canvas_h), "#FFFFFF")
        draw = ImageDraw.Draw(img)

        x0 = outer_pad
        y0 = outer_pad
        table_bottom = canvas_h - outer_pad

        draw.rectangle([x0, y0, x0 + table_w, y0 + header_row_h], fill="#1E293B")
        col_x = x0
        for c in range(ncols):
            y_txt = y0 + pad_y
            for ln in wrapped_h[c]:
                draw.text((col_x + pad_x, y_txt), ln, font=font_h, fill="#FFFFFF")
                y_txt += line_h_h
            col_x += col_widths[c]

        cur_y = y0 + header_row_h
        for r_idx, (wrapped_row, row_h) in enumerate(rendered_rows):
            bg = "#FFFFFF" if r_idx % 2 == 0 else "#F8FAFC"
            draw.rectangle([x0, cur_y, x0 + table_w, cur_y + row_h], fill=bg)
            col_x = x0
            for c in range(ncols):
                y_txt = cur_y + pad_y
                for ln in wrapped_row[c]:
                    draw.text((col_x + pad_x, y_txt), ln, font=font_b, fill="#0F172A")
                    y_txt += line_h_b
                col_x += col_widths[c]
            draw.line([(x0, cur_y), (x0 + table_w, cur_y)], fill="#CBD5E1", width=1)
            cur_y += row_h

        if notice_text:
            draw.rectangle([x0, cur_y, x0 + table_w, cur_y + notice_h], fill="#F1F5F9")
            draw.line([(x0, cur_y), (x0 + table_w, cur_y)], fill="#CBD5E1", width=1)
            draw.text((x0 + pad_x, cur_y + pad_y), notice_text, font=font_b, fill="#475569")

        grid_bottom = cur_y if notice_text else table_bottom
        col_x = x0
        for c in range(ncols - 1):
            col_x += col_widths[c]
            draw.line([(col_x, y0), (col_x, y0 + header_row_h)], fill="#334155", width=1)
            draw.line([(col_x, y0 + header_row_h), (col_x, grid_bottom)], fill="#E2E8F0", width=1)

        draw.rectangle([x0, y0, x0 + table_w, table_bottom], outline="#94A3B8", width=2)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception as exc:
        logger.warning("Table PNG render failed: %s", exc)
        return None
