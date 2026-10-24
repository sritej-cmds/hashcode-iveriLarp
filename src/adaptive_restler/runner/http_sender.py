"""Real HTTP sender for the executor's RequestSender protocol (stdlib only, loopback by default).

Does not follow redirects. HTTP error statuses are returned as responses, not raised. Timeouts raise
TimeoutError (the executor maps that to TIMED_OUT); any other transport error propagates (-> FAILED).
Only status_code, elapsed_seconds and content_length are returned; bodies are never retained.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Iterable
from urllib.parse import urlencode, urlsplit


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class UrllibSender:
    def __init__(self, allowed_hosts: Iterable[str] = ("127.0.0.1", "localhost"), max_read_bytes: int = 1_000_000) -> None:
        self._hosts = frozenset(h.lower() for h in allowed_hosts)
        self._max = max_read_bytes
        self._opener = urllib.request.build_opener(_NoRedirect)

    def send(self, *, method: str, url: str, headers: dict[str, str], query: dict[str, Any],
             body: Any, timeout_seconds: float) -> dict[str, Any]:
        parts = urlsplit(url)
        if parts.scheme != "http" or (parts.hostname or "").lower() not in self._hosts:
            raise ValueError("sender refuses this target")
        if query:
            url = url + ("&" if parts.query else "?") + urlencode({k: str(v) for k, v in query.items()})
        hdrs = dict(headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            if not any(k.lower() == "content-type" for k in hdrs):
                hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        start = time.perf_counter()
        try:
            with self._opener.open(req, timeout=timeout_seconds) as resp:
                status, content = resp.status, resp.read(self._max)
        except urllib.error.HTTPError as exc:
            status, content = exc.code, exc.read(self._max)
            exc.close()
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError("request timed out") from None
            raise
        return {"status_code": status, "elapsed_seconds": round(time.perf_counter() - start, 6),
                "content_length": len(content)}
