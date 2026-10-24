import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from adaptive_restler.selection.provider import (
    FakeProvider,
    ModelResponse,
    OllamaProvider,
    ProviderError,
    ProviderTimeout,
)

KW = dict(
    model="m", system="s", prompt="p", max_output_tokens=10,
    timeout_seconds=2.0, temperature=0.0, seed=0, context_tokens=512,
)


def reply(handler, status, payload: bytes):
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


@contextmanager
def serve(fn):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                fn(self, json.loads(body))
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_success_parses_text_and_tokens_and_sends_expected_request():
    seen = []

    def fn(h, body):
        seen.append(body)
        reply(h, 200, json.dumps(
            {"response": '{"proposals": []}', "prompt_eval_count": 7, "eval_count": 3}
        ).encode())

    with serve(fn) as url:
        r = OllamaProvider(url).generate(**KW)
    assert r.text == '{"proposals": []}'
    assert (r.prompt_tokens, r.completion_tokens) == (7, 3) and r.latency_seconds >= 0
    sent = seen[0]
    assert sent["model"] == "m" and sent["stream"] is False and sent["format"] == "json"
    assert sent["options"] == {"num_predict": 10, "temperature": 0.0, "seed": 0, "num_ctx": 512}


def test_http_error_is_provider_error():
    with serve(lambda h, b: reply(h, 500, b"boom")) as url:
        with pytest.raises(ProviderError, match="500") as info:
            OllamaProvider(url).generate(**KW)
    assert not isinstance(info.value, ProviderTimeout)


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"nope": 1}', b'{"response": 5}'])
def test_unusable_body_is_provider_error(body):
    with serve(lambda h, b: reply(h, 200, body)) as url:
        with pytest.raises(ProviderError):
            OllamaProvider(url).generate(**KW)


def test_slow_server_times_out():
    def fn(h, body):
        time.sleep(1.0)
        reply(h, 200, b'{"response": "x"}')

    with serve(fn) as url:
        with pytest.raises(ProviderTimeout):
            OllamaProvider(url).generate(**{**KW, "timeout_seconds": 0.2})


def test_unreachable_server_is_provider_error_not_timeout():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(ProviderError) as info:
        OllamaProvider(f"http://127.0.0.1:{port}").generate(**{**KW, "timeout_seconds": 10.0})
    assert not isinstance(info.value, ProviderTimeout)


def test_base_url_scheme_is_checked():
    with pytest.raises(ValueError):
        OllamaProvider("ftp://localhost")


def test_fake_provider_scripts_and_records_calls():
    fake = FakeProvider([ModelResponse(text="a"), "b", ProviderTimeout("t")])
    assert fake.generate(**KW).text == "a"
    assert fake.generate(**KW).text == "b"
    with pytest.raises(ProviderTimeout):
        fake.generate(**KW)
    assert fake.generate(**KW).text == '{"proposals": []}'
    assert len(fake.calls) == 4 and fake.calls[0]["model"] == "m"