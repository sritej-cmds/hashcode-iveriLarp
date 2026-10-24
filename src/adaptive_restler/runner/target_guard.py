"""Deterministic target allowlist check.

The allowlist comes from trusted configuration, never from the LLM.
Exact host matching only: no wildcards, no suffix matching.
Error messages never echo the full URL (it may contain secrets).
"""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


class TargetNotAllowedError(Exception):
    """Raised when a URL is not an explicitly authorized target."""


def _parse_entry(entry: str) -> tuple[str, int | None]:
    entry = entry.strip().lower()
    if not entry or any(c in entry for c in "/@*\\ "):
        raise ValueError("Invalid allowlist entry")
    parts = urlsplit("//" + entry)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid allowlist entry port") from exc
    if not parts.hostname:
        raise ValueError("Invalid allowlist entry")
    return parts.hostname.rstrip("."), port


def check_target_url(
    url: str,
    allowed_hosts: Iterable[str],
    allowed_schemes: Iterable[str] = ("http", "https"),
) -> None:
    """Return None if allowed, else raise TargetNotAllowedError.

    allowed_hosts entries: "host" (any port) or "host:port" (that port only).
    """
    if not isinstance(url, str) or any(c.isspace() or c == "\\" for c in url):
        raise TargetNotAllowedError("Malformed URL")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise TargetNotAllowedError("Malformed URL") from exc

    scheme = parts.scheme.lower()
    if scheme not in set(allowed_schemes):
        raise TargetNotAllowedError(f"Scheme not allowed: {scheme or '(none)'}")
    if not host:
        raise TargetNotAllowedError("URL has no host")
    if parts.username or parts.password:
        raise TargetNotAllowedError("Credentials in URL are not allowed")

    host = host.rstrip(".")
    effective_port = port if port is not None else _DEFAULT_PORTS.get(scheme)

    for entry in allowed_hosts:
        entry_host, entry_port = _parse_entry(entry)
        if host == entry_host and (entry_port is None or entry_port == effective_port):
            return
    raise TargetNotAllowedError(f"Target host not in allowlist: {host}")