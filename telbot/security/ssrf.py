"""SSRF protection for outbound HTTP requests and redirect validation."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse

import httpx

_BLOCKED_EXACT_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
    "metadata.goog",
}
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".home.arpa",
)


def is_ip_globally_routable(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True only if an IP (including unwrapped IPv4-mapped IPv6) is public and routable."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_ip_globally_routable(ip.ipv4_mapped)
        if ip.teredo is not None:
            return False
        if ip.sixtofour is not None and not is_ip_globally_routable(ip.sixtofour):
            return False

    return bool(
        ip.is_global
        and not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    )


def is_safe_public_url(
    url: str,
    *,
    blocked_hosts: Iterable[str] = (),
    allowed_ports: Iterable[int] = (80, 443),
    resolve_fn: Callable[..., list] = socket.getaddrinfo,
) -> bool:
    """Validate that a URL targets a public HTTP(S) host and never internal/private/metadata IPs."""
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False

    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or "%" in host:
        return False

    try:
        port = parsed.port
    except ValueError:
        return False
    allowed_port_set = set(allowed_ports)
    if port is not None and port not in allowed_port_set:
        return False

    blocked_exact = _BLOCKED_EXACT_HOSTS | {h.lower().rstrip(".") for h in blocked_hosts if h}
    if host in blocked_exact or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return False

    try:
        literal_ip = ipaddress.ip_address(host)
        return is_ip_globally_routable(literal_ip)
    except ValueError:
        pass

    try:
        infos = resolve_fn(host, None)
    except OSError:
        return False

    if not infos:
        return False

    checked_any = False
    for info in infos:
        sockaddr = info[4] if len(info) >= 5 else None
        if not sockaddr:
            return False
        raw_addr = str(sockaddr[0])
        if "%" in raw_addr:
            return False
        try:
            resolved_ip = ipaddress.ip_address(raw_addr)
        except ValueError:
            return False
        if not is_ip_globally_routable(resolved_ip):
            return False
        checked_any = True

    return checked_any


async def is_safe_public_url_async(
    url: str,
    *,
    blocked_hosts: Iterable[str] = (),
    allowed_ports: Iterable[int] = (80, 443),
) -> bool:
    """Run public URL / DNS SSRF validation off the asyncio event loop."""
    return await asyncio.to_thread(
        is_safe_public_url,
        url,
        blocked_hosts=tuple(blocked_hosts),
        allowed_ports=tuple(allowed_ports),
    )


def make_redirect_guard(
    *,
    blocked_hosts: Iterable[str] = (),
    allowed_ports: Iterable[int] = (80, 443),
):
    """Return an httpx response hook that blocks redirects to unsafe or private hosts."""
    blocked_tuple = tuple(blocked_hosts)
    ports_tuple = tuple(allowed_ports)

    async def _reject_unsafe_redirect(resp: httpx.Response) -> None:
        if resp.is_redirect:
            location = resp.headers.get("location", "")
            if not location:
                return
            target = urljoin(str(resp.url), location)
            if not await is_safe_public_url_async(
                target,
                blocked_hosts=blocked_tuple,
                allowed_ports=ports_tuple,
            ):
                raise httpx.HTTPError("Blocked unsafe redirect target")

    return _reject_unsafe_redirect
