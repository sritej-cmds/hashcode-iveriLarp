import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from adaptive_restler.runner.http_sender import UrllibSender


class _H(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def _reply(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        _H.seen.append((self.command, self.path, body, self.headers.get("Content-Type")))
        if self.path.startswith("/slow"):
            time.sleep(1.0)
        route = self.path.split("?")[0]
        code = {"/boom": 500, "/redir": 307}.get(route, 200)
        self.send_response(code)
        if code == 307:
            self.send_header("Location", "/ok")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST = do_PUT = do_DELETE = _reply


@pytest.fixture()
def base():
    _H.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _send(url, **kw):
    args = dict(method="GET", url=url, headers={}, query={}, body=None, timeout_seconds=5.0)
    args.update(kw)
    return UrllibSender().send(**args)


def test_500_is_a_response_not_an_exception(base):
    r = _send(base + "/boom")
    assert r["status_code"] == 500 and r["content_length"] == 2 and r["elapsed_seconds"] >= 0


def test_redirect_not_followed(base):
    assert _send(base + "/redir")["status_code"] == 307
    assert len(_H.seen) == 1


def test_query_and_json_body_sent(base):
    r = _send(base + "/ok", method="POST", query={"a": "1"}, body={"x": 1})
    assert r["status_code"] == 200
    method, path, body, ctype = _H.seen[0]
    assert (method, path, ctype) == ("POST", "/ok?a=1", "application/json")
    assert body == b'{"x": 1}'


def test_timeout_raises_timeouterror(base):
    with pytest.raises(TimeoutError):
        _send(base + "/slow", timeout_seconds=0.2)


def test_non_loopback_and_non_http_refused():
    with pytest.raises(ValueError):
        _send("http://example.com/x")
    with pytest.raises(ValueError):
        _send("https://127.0.0.1/x")
