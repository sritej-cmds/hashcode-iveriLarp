"""Artifact paths and secret redaction.

Artifacts are laid out as <base>/<run_id>/<candidate_id>/<name>.json so every
file is traceable to a run and candidate. IDs are validated to block path
traversal. Everything written is redacted first.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SENSITIVE_SUBSTRINGS = (
    "authorization",
    "cookie",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "api-key",
    "access_token",
    "refresh_token",
    "auth_token",
    "id_token",
    "credential",
)
_SENSITIVE_EXACT = {"token"}
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")


def _check_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value) or ".." in value:
        raise ValueError(f"Invalid {label}")
    return value


def artifact_dir(base_dir: str | Path, run_id: str, candidate_id: str | None = None) -> Path:
    path = Path(base_dir) / _check_id(run_id, "run_id")
    if candidate_id is not None:
        path = path / _check_id(candidate_id, "candidate_id")
    return path


def _is_sensitive_key(key: Any) -> bool:
    k = str(key).lower()
    return k in _SENSITIVE_EXACT or any(s in k for s in _SENSITIVE_SUBSTRINGS)


def redact(obj: Any) -> Any:
    """Recursively redact sensitive keys and bearer tokens. Returns a copy."""
    if isinstance(obj, dict):
        return {
            k: (REDACTED if _is_sensitive_key(k) else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return _BEARER_RE.sub(f"Bearer {REDACTED}", obj)
    return obj


def write_json_artifact(
    base_dir: str | Path,
    run_id: str,
    candidate_id: str | None,
    name: str,
    data: Any,
) -> Path:
    """Write redacted JSON atomically and return its path."""
    directory = artifact_dir(base_dir, run_id, candidate_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{_check_id(name, 'name')}.json"
    tmp = directory / f".{target.name}.tmp"
    tmp.write_text(
        json.dumps(redact(data), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return target