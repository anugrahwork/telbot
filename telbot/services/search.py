"""Paywall-skipping accessible web search, SSRF-safe 4-stage URL reader, and citation verifier."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from telbot.config import Settings
from telbot.security.ssrf import is_safe_public_url_async, make_redirect_guard

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")

_SEARCH_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "what", "when", "where", "who", "how", "why",
    "are", "was", "were", "can", "could", "about", "into", "your", "have", "has", "will", "latest",
    "current", "today", "now", "news", "search", "lookup", "please", "find", "tell", "give", "yang",
    "dan", "untuk", "dari", "apa", "siapa", "kapan", "dimana", "bagaimana", "tentang", "terbaru",
    "hari", "ini", "sekarang", "cari", "tolong",
}

ACADEMIC_QUERY_PATTERN = re.compile(
    r"\b(journal|paper|study|studies|research|doi|pubmed|clinical|trial|peer-reviewed|academic|citation|jurnal|penelitian|ilmiah)\b",
    re.IGNORECASE,
)

PAYWALL_OR_LOGIN_DOMAINS = (
    "wsj.com", "ft.com", "economist.com", "bloomberg.com", "nytimes.com",
    "washingtonpost.com", "barrons.com", "seekingalpha.com", "statista.com",
    "sciencedirect.com", "jstor.org", "tandfonline.com", "onlinelibrary.wiley.com",
    "link.springer.com", "academia.edu", "researchgate.net", "scribd.com",
    "chegg.com", "coursehero.com", "medium.com", "patreon.com", "substack.com",
    "linkedin.com", "instagram.com", "facebook.com", "x.com", "twitter.com",
)

_LOGIN_OR_PAYWALL_PATH_RE = re.compile(
    r"(?:^|/)(?:login|signin|sign-in|sign_in|wp-login\.php|auth|subscribe|subscription|paywall|checkout|register|signup|membership|sso)(?:$|/|\.)",
    re.IGNORECASE,
)

TRUSTED_OPEN_DOMAINS = (
    "who.int", "nih.gov", "cdc.gov", "pmc.ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov",
    "arxiv.org", "plos.org", "doaj.org", "europepmc.org", "biorxiv.org", "medrxiv.org",
    "wikipedia.org", "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "npr.org",
    "docs.python.org", "developer.mozilla.org", "github.com", "w3.org", "ietf.org", "iso.org",
)

PAYWALL_MARKERS = (
    "sign in to",
    "log in to",
    "sign in or register",
    "log in or register",
    "subscribe to read",
    "subscribe to continue",
    "subscribe now to",
    "subscriber-only",
    "subscribers only",
    "institutional access",
    "purchase pdf",
    "create a free account to read",
    "subscription required",
    "member-only story",
    "already a subscriber",
    "unlock this article",
    "register to continue reading",
    "sign up to continue",
    "for full access",
    "buy article",
    "rent this article",
)


_SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
_BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "br", "article", "section", "div", "blockquote",
}


def clean_url_token(raw_url: str) -> str:
    """Trim trailing sentence punctuation while preserving balanced parentheses in URLs."""
    url = (raw_url or "").strip().rstrip(".,;:!?>\"'")
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1].rstrip(".,;:!?>\"'")
    return url


class _TextExtractor(HTMLParser):
    """Stdlib HTML scraper that skips scripts/styles cleanly and preserves readable structure."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._parts: list[str] = []
        self._meta_desc = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if t == "meta" and not self._meta_desc:
            attr_map = {k.lower(): (v or "").strip() for k, v in attrs if k}
            name = attr_map.get("name", "").lower()
            prop = attr_map.get("property", "").lower()
            if name == "description" or prop == "og:description":
                self._meta_desc = attr_map.get("content", "")
        elif t in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth == 0 and t in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            text = re.sub(r"\s+", " ", data).strip()
            if text:
                self._parts.append(text + " ")

    def get_text(self) -> str:
        joined = "".join(self._parts)
        lines = [re.sub(r" +", " ", ln).strip() for ln in joined.splitlines()]
        body = "\n".join(ln for ln in lines if ln).strip()
        if self._meta_desc and self._meta_desc not in body[:400]:
            return f"{self._meta_desc}\n\n{body}".strip() if body else self._meta_desc
        return body


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith(f".{d}") for d in domains)


def extract_query_keywords(query: str) -> list[str]:
    words = [w.lower() for w in re.findall(r"[a-zA-Z0-9]{3,}", query)]
    return [w for w in dict.fromkeys(words) if w not in _SEARCH_STOPWORDS]


def is_paywalled_or_login_url(url: str) -> bool:
    """Return True if a URL points to a known paywalled domain or login/subscription route."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if host and _host_matches(host, PAYWALL_OR_LOGIN_DOMAINS):
        return True
    path_lower = parsed.path.lower()
    if _host_matches(host, ("wikipedia.org", "wiktionary.org")) and path_lower.startswith("/wiki/"):
        return "special:userlogin" in path_lower
    return bool(_LOGIN_OR_PAYWALL_PATH_RE.search(path_lower))


def is_paywalled_or_login_item(url: str, title: str = "", content: str = "") -> bool:
    if not url or is_paywalled_or_login_url(url):
        return True
    blob = f"{title} {content}".lower()
    return any(marker in blob for marker in PAYWALL_MARKERS)


def has_paywall_text(text: str) -> bool:
    head = text[:1500].lower()
    return any(marker in head for marker in PAYWALL_MARKERS)


def trusted_domain_bonus(url: str) -> float:
    """Boost ranking score for openly accessible official, academic, docs, and public news domains."""
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return 0.0
    if not host:
        return 0.0
    if (
        _host_matches(host, TRUSTED_OPEN_DOMAINS)
        or host.endswith((".gov", ".edu", ".org"))
        or any(part in host for part in (".gov.", ".edu.", ".ac."))
    ):
        return 0.45
    return 0.0


def filter_search_results(
    query: str,
    results: list[dict],
    min_score: float = 0.35,
    relaxed: bool = False,
) -> list[dict]:
    """Filter paywalled/login, low-score, or weak-overlap items and boost trusted open sources."""
    keywords = extract_query_keywords(query)
    effective_min_score = 0.20 if relaxed else min_score
    if not keywords:
        min_overlap = 0
    elif relaxed:
        min_overlap = 1
    else:
        min_overlap = 2 if len(keywords) >= 3 else 1

    scored: list[tuple[float, dict]] = []
    for item in results:
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "")
        content = str(item.get("content") or "")
        if is_paywalled_or_login_item(url, title, content):
            continue
        score = float(item.get("score") or 0.5)
        if "score" in item and float(item["score"] or 0.0) < effective_min_score:
            continue
        blob = f"{title} {content}".lower()
        overlap = sum(1 for kw in keywords if kw in blob) if keywords else 1
        if overlap < min_overlap:
            continue
        scored.append((score + overlap * 0.25 + trusted_domain_bonus(url), item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored]


def _decode_ddg_href(raw_href: str) -> str:
    unescaped = html.unescape(raw_href.strip())
    if unescaped.startswith("//"):
        unescaped = f"https:{unescaped}"
    parsed = urlparse(unescaped)
    if "duckduckgo.com" in (parsed.hostname or "").lower() and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        uddg = qs.get("uddg", [])
        if uddg:
            return unquote(uddg[0])
    return unescaped


def parse_duckduckgo_results(raw_html: str) -> list[dict]:
    """Extract structured {title, url, content} items from DuckDuckGo HTML search output."""
    anchor_re = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
    class_re = re.compile(r"""\bclass\s*=\s*["'][^"']*\bresult__a\b[^"']*["']""", re.IGNORECASE)
    href_re = re.compile(r"""\bhref\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
    snippet_re = re.compile(
        r"""class\s*=\s*["'][^"']*\bresult__snippet\b[^"']*["'][^>]*>(.*?)</(?:a|div|span)>""",
        re.IGNORECASE | re.DOTALL,
    )

    matched_links: list[tuple[int, int, str, str]] = []
    for m in anchor_re.finditer(raw_html):
        attrs = m.group(1)
        if not class_re.search(attrs):
            continue
        href_m = href_re.search(attrs)
        if not href_m:
            continue
        url = _decode_ddg_href(href_m.group(1))
        if not url.startswith(("http://", "https://")):
            continue
        title = html.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))
        title = re.sub(r"\s+", " ", title).strip()
        matched_links.append((m.start(), m.end(), url, title))

    results: list[dict] = []
    for idx, (_, end_anchor, url, title) in enumerate(matched_links):
        next_start = matched_links[idx + 1][0] if idx + 1 < len(matched_links) else len(raw_html)
        chunk = raw_html[end_anchor:next_start]
        sm = snippet_re.search(chunk)
        snippet = ""
        if sm:
            snippet = html.unescape(re.sub(r"<[^>]+>", " ", sm.group(1)))
            snippet = re.sub(r"\s+", " ", snippet).strip()
        results.append({"title": title, "url": url, "content": snippet})
    return results


def url_to_search_query(url: str) -> str:
    parsed = urlparse(url)
    tokens = re.findall(r"[a-zA-Z0-9]{2,}", unquote(f"{parsed.path} {parsed.query}"))
    noise = {
        "www", "http", "https", "html", "htm", "php", "aspx", "jsp", "article", "articles",
        "story", "stories", "news", "index", "page", "login", "signin", "subscribe", "auth", "pii", "abs",
    }
    clean_tokens = [t for t in tokens if t.lower() not in noise and not t.isdigit()]
    if len(clean_tokens) >= 2:
        return " ".join(clean_tokens[:10])
    return (parsed.hostname or url).replace("www.", "")


def _extract_html_title(raw_html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    lower = title.lower()
    bad_titles = ("access denied", "just a moment", "sign in", "log in", "login", "subscribe", "403", "404", "cloudflare", "error")
    if len(title) < 8 or any(b in lower for b in bad_titles):
        return ""
    return re.split(r"\s+[|\-–—]\s+(?=[^|\-–—]{2,30}$)", title)[0].strip()


class SearchService:
    """Provides paywall-skipping live search, SSRF-safe link reading, and citation verification."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._redirect_hook = make_redirect_guard(
            blocked_hosts=settings.blocked_ssrf_hosts,
            allowed_ports=settings.allowed_web_ports,
        )

    async def _is_safe_url(self, url: str) -> bool:
        return await is_safe_public_url_async(
            url,
            blocked_hosts=self.settings.blocked_ssrf_hosts,
            allowed_ports=self.settings.allowed_web_ports,
        )

    async def _check_url_accessible(self, client: httpx.AsyncClient, url: str) -> bool:
        if is_paywalled_or_login_url(url) or not await self._is_safe_url(url):
            return False
        try:
            resp = await client.head(url)
            final_url = str(resp.url)
            if is_paywalled_or_login_url(final_url):
                return False
            if resp.status_code in (403, 405, 501):
                async with client.stream("GET", url) as get_resp:
                    return get_resp.status_code < 400 and not is_paywalled_or_login_url(str(get_resp.url))
            return resp.status_code < 400
        except Exception:
            return False

    async def fetch_crossref_papers(self, query: str, rows: int = 6) -> list[str]:
        url = f"https://api.crossref.org/works?query={quote_plus(query)}&rows={rows}"
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(url, headers={"User-Agent": "Telbot/1.0"})
                resp.raise_for_status()
                items = resp.json().get("message", {}).get("items", []) or []
                lines: list[str] = []
                for it in items:
                    titles = it.get("title") or []
                    title = titles[0] if titles else ""
                    doi = it.get("DOI", "")
                    licenses = it.get("license") or []
                    is_cc_open = any("creativecommons.org" in str(lic.get("URL", "")).lower() for lic in licenses)
                    if not (title and doi and is_cc_open):
                        continue

                    authors = it.get("author") or []
                    first_author = authors[0].get("family", "") if authors else ""
                    year_parts = ((it.get("published") or {}).get("date-parts") or [[]])[0]
                    year = str(year_parts[0]) if year_parts else ""
                    meta = ", ".join(p for p in (first_author, year) if p)
                    meta_str = f" ({meta})" if meta else ""

                    target_url = f"https://doi.org/{doi}"
                    for link_obj in it.get("link") or []:
                        cand_link = str(link_obj.get("URL") or "").strip()
                        if cand_link.startswith(("http://", "https://")) and not is_paywalled_or_login_url(cand_link):
                            target_url = cand_link
                            break

                    raw_abstract = str(it.get("abstract") or "")
                    clean_abstract = re.sub(r"<[^>]+>", " ", raw_abstract)
                    clean_abstract = re.sub(r"\s+", " ", clean_abstract).strip()[:450]
                    abs_part = f"\n  Abstract: {clean_abstract}" if clean_abstract else ""
                    lines.append(f"- [Open-Access] [Crossref DOI] {title}{meta_str}: {target_url}{abs_part}")
                return lines
        except Exception as exc:
            logger.debug("Crossref lookup failed for '%s': %s", query, exc)
            return []

    async def _verify_and_select(self, items: list[dict], crossref_list: list[str]) -> tuple[list[dict], list[str]]:
        web_cands = items[:6]
        cr_cands = crossref_list[:4]
        cr_urls: list[str] = []
        for entry in cr_cands:
            found = URL_PATTERN.findall(entry)
            cr_urls.append(clean_url_token(found[0]) if found else "")

        all_urls = [str(it.get("url") or "") for it in web_cands] + cr_urls
        if not any(all_urls):
            return [], []

        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=5,
            timeout=httpx.Timeout(3.0, connect=1.8),
            headers={"User-Agent": "Mozilla/5.0"},
            event_hooks={"response": [self._redirect_hook]},
        ) as client:
            checks = await asyncio.gather(
                *(self._check_url_accessible(client, u) if u else asyncio.sleep(0, result=False) for u in all_urls)
            )

        web_ok = [it for it, ok in zip(web_cands, checks[: len(web_cands)]) if ok]
        cr_ok = [line for line, ok in zip(cr_cands, checks[len(web_cands) :]) if ok]
        return web_ok, cr_ok

    async def search_web_live(self, query: str, max_chars: int = 4000) -> str | None:
        """Search via Tavily (if configured) or DuckDuckGo + Crossref, filtering paywalls and verifying links."""
        if not self.settings.enable_web_search:
            return None

        is_academic = bool(ACADEMIC_QUERY_PATTERN.search(query))

        async def _tavily_search(search_q: str) -> tuple[str, list[dict], bool]:
            if not self.settings.tavily_api_key:
                return "", [], False
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        "https://api.tavily.com/search",
                        json={
                            "api_key": self.settings.tavily_api_key,
                            "query": search_q,
                            "search_depth": "advanced",
                            "include_answer": True,
                            "max_results": 10,
                            "exclude_domains": list(PAYWALL_OR_LOGIN_DOMAINS),
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    return str(data.get("answer") or ""), list(data.get("results") or []), True
            except Exception as exc:
                logger.warning("Tavily search failed, falling back to DuckDuckGo: %s", exc)
                return "", [], False

        (tavily_answer, raw_results, tavily_ok), crossref_lines = await asyncio.gather(
            _tavily_search(query),
            self.fetch_crossref_papers(query) if is_academic else asyncio.sleep(0, result=[]),
        )

        if tavily_ok or crossref_lines:
            filtered = filter_search_results(query, raw_results) if raw_results else []
            if not filtered and raw_results:
                filtered = filter_search_results(query, raw_results, relaxed=True)
            verified_web, verified_cr = await self._verify_and_select(filtered, crossref_lines)

            if not verified_web and not verified_cr and self.settings.tavily_api_key and tavily_ok:
                alt_answer, alt_raw, alt_ok = await _tavily_search(f"{query} official OR open access")
                if alt_ok and alt_raw:
                    alt_filtered = filter_search_results(query, alt_raw, relaxed=True)
                    verified_web, _ = await self._verify_and_select(alt_filtered, [])
                    if verified_web and alt_answer and not tavily_answer:
                        tavily_answer = alt_answer

            combined = self._format_verified_results(tavily_answer, verified_web, verified_cr)
            if combined:
                return combined[:max_chars]
            if tavily_ok:
                return None

        # Zero-key DuckDuckGo HTML fallback with full paywall filtering + verification
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=5,
                timeout=8.0,
                event_hooks={"response": [self._redirect_hook]},
            ) as client:
                resp = await client.get(
                    search_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
                resp.raise_for_status()
                ddg_items = parse_duckduckgo_results(resp.text)
                if ddg_items:
                    filtered_ddg = filter_search_results(query, ddg_items) or filter_search_results(query, ddg_items, relaxed=True)
                    verified_ddg, verified_cr = await self._verify_and_select(filtered_ddg, crossref_lines)
                    formatted = self._format_verified_results("", verified_ddg, verified_cr)
                    if formatted:
                        return formatted[:max_chars]
                parser = _TextExtractor()
                parser.feed(resp.text)
                text = parser.get_text()
                return text[:max_chars] if text and not has_paywall_text(text) else None
        except Exception as exc:
            logger.warning("DuckDuckGo web search failed for '%s': %s", query, exc)
            return None

    @staticmethod
    def _format_verified_results(summary: str, web_items: list[dict], cr_lines: list[str]) -> str:
        parts: list[str] = []
        if cr_lines:
            parts.append("Verified Open-Access Academic Sources (Crossref):\n" + "\n".join(cr_lines))
        if web_items:
            if summary:
                parts.append(f"Summary: {summary}")
            for item in web_items:
                title = item.get("title", "")
                content = item.get("content", "")
                url = item.get("url", "")
                pub_date = item.get("published_date", "")
                date_tag = f" [{pub_date}]" if pub_date else ""
                parts.append(f"- {title}{date_tag} ({url}): {content}")
        return "\n".join(parts).strip()

    async def fetch_url_preview(self, url: str, max_chars: int = 4000) -> str | None:
        """Read accessible public URLs; skip paywalled/login URLs and find open trusted alternatives."""
        if not self.settings.enable_url_reader:
            return None
        if not await self._is_safe_url(url):
            return None

        saw_paywall = is_paywalled_or_login_url(url)
        saw_403 = False
        extracted_title = ""

        candidates = [url]
        if self.settings.enable_jina_reader:
            candidates.append(f"https://r.jina.ai/{url}")

        if not saw_paywall:
            for candidate_url in candidates:
                try:
                    async with httpx.AsyncClient(
                        follow_redirects=True,
                        max_redirects=5,
                        timeout=8.0,
                        event_hooks={"response": [self._redirect_hook]},
                    ) as client:
                        async with client.stream("GET", candidate_url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                            if resp.status_code == 401 or is_paywalled_or_login_url(str(resp.url)):
                                saw_paywall = True
                                break
                            if resp.status_code == 403:
                                saw_403 = True
                                continue
                            resp.raise_for_status()
                            content_type = resp.headers.get("content-type", "").lower()
                            if not ("text/" in content_type or "json" in content_type or "xml" in content_type):
                                continue

                            chunks = bytearray()
                            async for chunk in resp.aiter_bytes():
                                chunks.extend(chunk)
                                if len(chunks) >= self.settings.max_url_bytes:
                                    break

                            raw_text = chunks.decode("utf-8", errors="replace")
                            if "text/html" in content_type and not candidate_url.startswith("https://r.jina.ai/"):
                                if not extracted_title:
                                    extracted_title = _extract_html_title(raw_text)
                                parser = _TextExtractor()
                                parser.feed(raw_text)
                                text = parser.get_text()
                            else:
                                text = raw_text

                            if has_paywall_text(text):
                                saw_paywall = True
                                break
                            if text and len(text.strip()) > 80:
                                return text.strip()[:max_chars]
                except Exception as exc:
                    logger.warning("URL fetch attempt failed for %s: %s", candidate_url, exc)

        if not saw_paywall and self.settings.tavily_api_key:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        "https://api.tavily.com/extract",
                        headers={"Authorization": f"Bearer {self.settings.tavily_api_key}"},
                        json={"urls": [url], "extract_depth": "advanced"},
                    )
                    resp.raise_for_status()
                    results = resp.json().get("results") or []
                    if results and isinstance(results, list):
                        raw_content = str(results[0].get("raw_content", "")).strip()
                        if raw_content:
                            if has_paywall_text(raw_content):
                                saw_paywall = True
                            else:
                                return raw_content[:max_chars]
            except Exception as exc:
                logger.warning("Tavily extract failed for %s: %s", url, exc)

        alt_query = extracted_title or url_to_search_query(url)
        alt_res = await self.search_web_live(alt_query, max_chars=max_chars)
        if alt_res and (saw_paywall or saw_403):
            reason = "paywalled or login-only" if saw_paywall else "blocked from direct automated access (HTTP 403)"
            prefix = (
                f"[Note: The original link was {reason} and skipped automatically. "
                "Answer using these verified accessible sources instead:]\n"
            )
            return f"{prefix}{alt_res}"[:max_chars]
        return alt_res

    async def verify_citations(self, text: str) -> str:
        """Concurrently verify cited URLs and strip any paywalled, private, or broken links."""
        raw_urls = [clean_url_token(u) for u in URL_PATTERN.findall(text)]
        deduped = [u for u in dict.fromkeys(raw_urls) if u]
        if not deduped:
            return text

        safety_flags = await asyncio.gather(*(self._is_safe_url(u) for u in deduped))
        unsafe_urls = {u for u, safe in zip(deduped, safety_flags) if not safe}
        safe_urls = [u for u, safe in zip(deduped, safety_flags) if safe][:8]

        bad_urls = set(unsafe_urls)
        if safe_urls:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=5,
                timeout=httpx.Timeout(3.5, connect=2.0),
                headers={"User-Agent": "Mozilla/5.0"},
                event_hooks={"response": [self._redirect_hook]},
            ) as client:
                checks = await asyncio.gather(*(self._check_url_accessible(client, u) for u in safe_urls))
            bad_urls.update(url for url, ok in zip(safe_urls, checks) if not ok)

        if not bad_urls:
            return text

        return strip_bad_citations(text, bad_urls)


def strip_bad_citations(text: str, bad_urls: set[str]) -> str:
    """Remove broken, private, or paywalled URLs cleanly while preserving readable prose."""
    if not bad_urls:
        return text

    balanced_url_pat = r"https?://(?:[^\s()]|\([^\s()]*\))+"
    pure_link_bullet_re = re.compile(
        r"^\s*(?:[-*•]|\d+\.)\s*(?:(?:source|link|see|ref(?:erence)?)[:\s-]*)?"
        rf"(?:\[[^\]]+\]\(({balanced_url_pat})\)|({balanced_url_pat}))[.,;:]*\s*$",
        re.IGNORECASE,
    )
    md_link_re = re.compile(rf"\[([^\]]+)\]\(({balanced_url_pat})\)")
    paren_url_re = re.compile(rf"\s*\(({balanced_url_pat})\)")

    def _strip_bare(match: re.Match[str]) -> str:
        raw = match.group(0)
        core = clean_url_token(raw)
        if core not in bad_urls:
            return raw
        return raw[len(core) :]

    kept_lines: list[str] = []
    for line in text.split("\n"):
        m_bullet = pure_link_bullet_re.match(line)
        if m_bullet:
            bullet_url = clean_url_token(m_bullet.group(1) or m_bullet.group(2) or "")
            if bullet_url in bad_urls:
                continue

        updated = md_link_re.sub(
            lambda m: m.group(1) if clean_url_token(m.group(2)) in bad_urls else m.group(0),
            line,
        )
        updated = paren_url_re.sub(
            lambda m: "" if clean_url_token(m.group(1)) in bad_urls else m.group(0),
            updated,
        )
        updated = URL_PATTERN.sub(_strip_bare, updated)
        updated = re.sub(r"[ \t]{2,}", " ", updated)
        updated = re.sub(r"\s+([.,;:!?])", r"\1", updated).rstrip()
        kept_lines.append(updated)

    cleaned = "\n".join(kept_lines)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()
