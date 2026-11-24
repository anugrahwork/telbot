"""Container healthcheck entrypoint for webhook and polling modes."""

from __future__ import annotations

import os
import urllib.request
from typing import Mapping


def run_healthcheck(
    env: Mapping[str, str] | None = None,
    *,
    container_check: bool | None = None,
    proc_cmdline_path: str = "/proc/1/cmdline",
) -> int:
    """Return 0 when healthy, non-zero otherwise."""
    src = env if env is not None else os.environ
    webhook_active = bool(
        (src.get("WEBHOOK_URL") or "").strip()
        or (src.get("RENDER_EXTERNAL_HOSTNAME") or "").strip()
        or (src.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    )
    if webhook_active:
        port = (src.get("PORT") or "10000").strip()
        url = f"http://127.0.0.1:{port}/health"
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                return 0 if 200 <= resp.status < 300 else 1
        except Exception:
            return 1

    if container_check is None:
        container_check = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
    if container_check and os.path.exists(proc_cmdline_path):
        try:
            with open(proc_cmdline_path, "rb") as f:
                return 0 if b"python" in f.read().lower() else 1
        except OSError:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run_healthcheck())
