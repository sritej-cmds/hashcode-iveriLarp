"""Model provider interface, a stdlib Ollama client, and a scripted fake.

Errors carry fixed messages only: never the prompt, response text or URL.
The Ollama client ignores system proxy settings (it targets a local server).
"""

import http.client
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "FakeProvider",
    "ModelProvider",
    "ModelResponse",
    "OllamaProvider",
    "ProviderError",
    "ProviderTimeout",
]

_MAX_BODY_BYTES = 1_000_000


class ProviderError(Exception):
    """The model provider failed or returned an unusable response."""


class ProviderTimeout(ProviderError):
    """The model provider did not answer in time."""


@dataclass(frozen=True)
class ModelResponse:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_seconds: float = 0.0


class ModelProvider(Protocol):
    def generate(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        max_output_tokens: int,
        timeout_seconds: float,
        temperature: float,
        seed: int,
        context_tokens: int,
    ) -> ModelResponse: ...


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class OllamaProvider:
    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        self.base_url = base_url.rstrip("/")
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def generate(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        max_output_tokens: int,
        timeout_seconds: float,
        temperature: float,
        seed: int,
        context_tokens: int,
    ) -> ModelResponse:
        payload = json.dumps(
            {
                "model": model,
                "system": system,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {
                    "num_predict": max_output_tokens,
                    "temperature": temperature,
                    "seed": seed,
                    "num_ctx": context_tokens,
                },
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        start = time.perf_counter()
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                body = response.read(_MAX_BODY_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"provider returned HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ProviderTimeout("provider timed out") from None
            raise ProviderError("provider unavailable") from None
        except (TimeoutError, socket.timeout):
            raise ProviderTimeout("provider timed out") from None
        except (OSError, http.client.HTTPException):
            raise ProviderError("provider unavailable") from None

        if len(body) > _MAX_BODY_BYTES:
            raise ProviderError("provider response too large")
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            raise ProviderError("invalid provider response") from None
        if not isinstance(data, dict) or not isinstance(data.get("response"), str):
            raise ProviderError("invalid provider response")
        return ModelResponse(
            text=data["response"],
            prompt_tokens=_opt_int(data.get("prompt_eval_count")),
            completion_tokens=_opt_int(data.get("eval_count")),
            latency_seconds=time.perf_counter() - start,
        )


class FakeProvider:
    """Scripted provider for tests. Items: str, ModelResponse, or an exception to raise."""

    def __init__(self, script: list | None = None) -> None:
        self._script = list(script or [])
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        max_output_tokens: int,
        timeout_seconds: float,
        temperature: float,
        seed: int,
        context_tokens: int,
    ) -> ModelResponse:
        self.calls.append(
            {
                "model": model,
                "system": system,
                "prompt": prompt,
                "max_output_tokens": max_output_tokens,
                "timeout_seconds": timeout_seconds,
                "temperature": temperature,
                "seed": seed,
                "context_tokens": context_tokens,
            }
        )
        item = self._script.pop(0) if self._script else '{"proposals": []}'
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, ModelResponse):
            return item
        return ModelResponse(text=item, prompt_tokens=10, completion_tokens=5, latency_seconds=0.01)