# Security Policy & Operator Hardening Guide

## Built-In Public-Safety Defenses

This bot is designed for open/community Telegram deployments where untrusted users, group links, and uploaded files interact with the bot:

1. **Fail-Closed Admin Access:** Leaving `ADMIN_USERS` empty grants **zero** admin privileges. Only numeric Telegram User IDs explicitly listed in `ADMIN_USERS` can run `/model`, `/ban`, `/unban`, or `/admin`.
2. **Per-User Rate Limits, Concurrency Locks & Daily Quotas:** Sliding-window burst (`3/10s`), user RPM (`6/min`), group RPM (`20/min`), 1 concurrent request per user, and persisted UTC daily limits (`50` messages, `15` searches, `5` images, `10` files by default) protect API spend.
3. **SSRF Protection (`telbot/security/ssrf.py`):**
   - Permits only `http://` and `https://` on ports `80`/`443` (configurable via `ALLOWED_WEB_PORTS`).
   - Rejects embedded URL credentials (`user:pass@host`).
   - Resolves DNS records and blocks loopback, private, link-local, multicast, CGNAT, cloud-metadata (`169.254.169.254`, `metadata.google.internal`), Teredo, and IPv4-mapped IPv6 addresses (`::ffff:127.0.0.1`), plus the bot's own webhook hostname.
   - Re-validates every HTTP redirect hop.
4. **Prompt-Injection Isolation (`telbot/security/sanitize.py`):**
   - External search results, fetched webpages, OCR text, and document contents are wrapped in per-request random-nonced `<untrusted_data_xxxxxxxx>` tags after neutralizing matching delimiter tags in untrusted text.
   - Permanent `[REMEMBER: ...]` profile facts are disabled in group chats so group members cannot poison shared group memory.
5. **Resource-Bounded Evaluators & Parsers:**
   - AST math evaluator (`telbot/security/safe_math.py`) limits expression length, AST nodes, exponent magnitude, and `factorial()` bounds.
   - `.docx` extraction (`telbot/services/documents.py`) checks uncompressed `word/document.xml` size against zip bombs and rejects `<!DOCTYPE` / `<!ENTITY` declarations.
   - Downloadable file names pass `sanitize_filename()` to block path traversal and executable extensions.
6. **Constant-Time Webhook Verification:** Webhook updates validate `X-Telegram-Bot-Api-Secret-Token` via `hmac.compare_digest`.

## Outbound Network Endpoints

Every external connection Telbot makes is transparent, documented, and gated by `.env` settings or SSRF validation:

| Destination | Purpose | Control / Disable Flag |
| :--- | :--- | :--- |
| `api.telegram.org` | Telegram Bot API (updates, messages, files) | Required (`BOT_TOKEN`) |
| `AI_BASE_URL` (default `api.openai.com`) & `FALLBACK_AI_BASE_URL` | Primary/fallback OpenAI-compatible LLM, Vision, and Image APIs | Configured by operator in `.env` |
| `html.duckduckgo.com` & `api.crossref.org` | Free open-web & academic open-access search | `ENABLE_WEB_SEARCH=false` |
| `r.jina.ai` | Clean webpage reader fallback for JS/403 pages | `ENABLE_JINA_READER=false` or `ENABLE_URL_READER=false` |
| User-provided public `http(s)://` URLs | Webpage summarization when reading shared links | `ENABLE_URL_READER=false` (always guarded by `telbot/security/ssrf.py`) |
| `api.tavily.com` | Optional enhanced search & extraction API | Inactive unless `TAVILY_API_KEY` is set |
| `api.cloudflare.com` | Optional Cloudflare Workers AI LLM/Vision/Image fallback | Inactive unless `CF_ACCOUNT_ID` & `CF_API_TOKEN` are set |
| `api.ocr.space` | Optional OCR text-extraction fallback | Inactive unless `OCR_API_KEY` is set (or `ENABLE_OCR_SPACE=false`) |
| `image.pollinations.ai` | Optional free image-generation fallback | `ENABLE_POLLINATIONS_FALLBACK=false` or `ENABLE_IMAGE_GEN=false` |
| `quickchart.io` | Chart PNG rendering for `<chart>` directives | `ENABLE_CHARTS=false` |
| `SUPABASE_URL` | Optional cloud memory/quota backup sync | Inactive unless `SUPABASE_URL` & `SUPABASE_KEY` are set |
| `127.0.0.1:<PORT>/health` | Loopback-only container health probe (`telbot/healthcheck.py`) | Webhook mode only (`WEBHOOK_URL` / cloud host) |

## Operator Deployment Checklist

- Never commit `.env` or `data/bot.db` to version control.
- Set `ADMIN_USERS` to your Telegram User ID (obtain via `/id`) before sharing the bot publicly.
- Use `GROUP_TRIGGER_MODE=mention_or_reply` in busy public groups so the bot only answers when `@mentioned`, replied to, `/ask`, or `/search`.
- Set `ALLOWED_GROUPS` if you only want the bot in specific community groups (it auto-leaves unauthorized groups).
- Keep `WEBHOOK_SECRET` set to a strong random string in production webhook deployments (or leave blank to auto-derive one from `BOT_TOKEN`).
- On Render/Railway free tiers where ephemeral disks reset on redeploy, attach a persistent disk/volume at `/app/data` (or configure `SUPABASE_URL` + `SUPABASE_KEY`) if you need memory & daily quotas to survive container rebuilds.

## Reporting Vulnerabilities

If you host a public fork and find a security issue, please open a private security advisory on your repository before disclosing exploit details publicly.
