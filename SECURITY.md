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

## Operator Deployment Checklist

- Never commit `.env` or `data/bot.db` to version control.
- Set `ADMIN_USERS` to your Telegram User ID (obtain via `/id`) before sharing the bot publicly.
- Use `GROUP_TRIGGER_MODE=mention_or_reply` in busy public groups so the bot only answers when `@mentioned`, replied to, `/ask`, or `/search`.
- Set `ALLOWED_GROUPS` if you only want the bot in specific community groups (it auto-leaves unauthorized groups).
- Keep `WEBHOOK_SECRET` set to a strong random string in production webhook deployments (or leave blank to auto-derive one from `BOT_TOKEN`).
- On Render/Railway free tiers where ephemeral disks reset on redeploy, attach a persistent disk/volume at `/app/data` (or configure `SUPABASE_URL` + `SUPABASE_KEY`) if you need memory & daily quotas to survive container rebuilds.

## Reporting Vulnerabilities

If you host a public fork and find a security issue, please open a private security advisory on your repository before disclosing exploit details publicly.
