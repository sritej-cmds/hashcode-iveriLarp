"""Offline importer for RESTler replay-log text (Milestone 2a, EXPERIMENTAL).

Scope
-----
Pure, offline parsing of RESTler *replay logs* into ``CandidateSequence``
objects. It never starts RESTler, never opens a socket, never sleeps and never
writes files. Imported candidates are executed (if at all) only through the
existing executor, so target allowlisting, request/time budgets, dry-run and
artifact redaction still apply.

Supported input: replay-log files only (one file == one sequence), in the
format documented on RESTler's "Replay" page: request lines starting with
``-> `` (escaped-CRLF headers, optional JSON body), ``! name value`` setting
lines and ``PREVIOUS RESPONSE: '...'`` lines. NOT supported: RESTler network
logs, trace databases, ``grammar.py``/``grammar.json``.

The parser is EXPERIMENTAL: it follows the documented format only and has not
been validated against a real captured replay log. Test fixtures are synthetic.

What an import does and does not prove
--------------------------------------
Replay logs only exist for sequences that already triggered a bug bucket, and
dynamic values (resource ids) are literal values from the original run.
Importing them exercises the pipeline; it says nothing about whether
LLM-guided selection improves fuzzing effectiveness.

Behaviour summary
-----------------
* Scheme, host and port come from ``TargetConfig.base_url`` only. The replay
  ``Host`` header must match it (effective port included) and is then dropped.
  ``base_url`` must have no path/query/fragment (join semantics are unverified).
* Every request URL is checked with ``check_target_url`` against
  ``target.allowed_hosts``.
* Kept headers: ``Accept`` and ``Content-Type`` only. Any other header is
  rejected (``unsupported_header``), never silently dropped. Credential-bearing
  headers follow ``AuthHeaderPolicy``: ``REJECT`` (default) rejects the
  sequence; ``MAP_TO_PROFILE`` drops them, records only their *names*, and sets
  ``auth_profile_id`` (must equal ``target.auth_profile_id``). Sequences with no
  credential header get ``auth_profile_id=None`` under both policies.
* Query strings and paths are restricted to unreserved characters
  (``A-Za-z0-9._~-``, plus ``/`` in paths) because the executor's encoding
  behaviour is unverified. Fragments and duplicate query keys are rejected.
* Bodies: JSON objects/arrays only, with a JSON ``Content-Type``. Backslash
  escapes *inside* JSON strings are rejected as ambiguous (the log's escaping
  of backslashes is not documented). Bodies with identifiable credential field
  names or credential-looking values are rejected (heuristic, not a guarantee).
* ``!`` settings and the recorded response status go to provenance only;
  nothing sleeps. Response bodies are never retained.
* Candidate ids are ``imp-`` + first 20 hex chars of the SHA-256 of a
  canonical JSON rendering of the sequence content (+ ``auth_profile_id``);
  independent of file name, path and target. First occurrence (sorted path
  order) wins; identical later content is rejected as ``duplicate_sequence``;
  an id equal to an earlier one with different content is rejected as
  ``candidate_id_collision``.
* Messages contain only stable codes, line numbers, counts and header names;
  never raw file content, URLs or credential values.
* Imports create no findings and no execution results.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from pydantic import ValidationError

from adaptive_restler.models import CandidateRequest, CandidateSequence, TargetConfig
from adaptive_restler.runner.target_guard import TargetNotAllowedError, check_target_url

PARSER_STATUS = "experimental"
REDACTED_VALUE = "[REDACTED]"

SUPPORTED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
SUPPORTED_SETTINGS = frozenset({"producer_timing_delay", "max_async_wait_time"})

REASON_CODES = frozenset(
    {
        # parse-level
        "no_requests", "invalid_characters", "request_line_malformed",
        "unsupported_http_version", "unsupported_method", "header_block_malformed",
        "header_malformed", "body_invalid_escape", "body_string_escape_unsupported",
        "body_unterminated_string", "body_control_character", "setting_malformed",
        "setting_unsupported", "setting_duplicate", "setting_without_request",
        "response_line_malformed", "response_without_request", "duplicate_response",
        # import-level
        "file_not_utf8", "too_many_requests", "body_too_large", "host_header_missing",
        "host_header_invalid", "host_mismatch", "target_not_allowed", "duplicate_header",
        "unsupported_header", "auth_material_present", "request_target_invalid",
        "url_fragment_not_allowed", "query_malformed", "query_duplicate_key",
        "query_unsupported_characters", "query_credential_field",
        "body_content_type_missing", "body_unsupported_content_type", "body_not_json",
        "body_unsupported_type", "body_duplicate_key", "body_too_deep",
        "body_credential_field", "candidate_validation_failed", "duplicate_sequence",
        "candidate_id_collision",
        # notes
        "auth_replaced_by_profile", "likely_requires_auth", "settings_not_applied",
    }
)

_CRLF = "\\r\\n"  # the four literal characters: backslash r backslash n
_REQUEST_RE = re.compile(r"^-> ([A-Z]+) (\S+) (HTTP/[0-9]\.[0-9])\\r\\n(.*)$")
_HEADER_RE = re.compile(r"^([!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}):[ \t]*([ -~]*?)[ \t]*$")
_SETTING_RE = re.compile(r"^! ([a-z_]{1,64}) ([0-9]{1,9}(?:\.[0-9]{1,9})?)$")
_RESPONSE_RE = re.compile(r"^PREVIOUS RESPONSE: 'HTTP/1\.[01] ([1-5][0-9]{2})(?: |\\r\\n)")
_PATH_RE = re.compile(r"/[A-Za-z0-9._~/-]*")
_QUERY_PART_RE = re.compile(r"[A-Za-z0-9._~-]*")
_MAX_HEADER_VALUE = 1024
_MAX_JSON_DEPTH = 32
_DEFAULT_PORTS = {"http": 80, "https": 443}
_ALLOWED_HEADERS = {"accept": "Accept", "content-type": "Content-Type"}

_CRED_HEADER_EXACT = frozenset({"authorization", "proxy-authorization", "cookie", "set-cookie"})
_CRED_HEADER_SUBSTRINGS = (
    "auth", "token", "secret", "password", "passwd", "credential", "cookie",
    "session", "api-key", "apikey", "api_key", "bearer", "jwt", "signature",
)
_CRED_KEY_SUBSTRINGS = (
    "password", "passwd", "secret", "token", "apikey", "authorization",
    "credential", "privatekey", "accesskey", "bearer", "sessionid", "cookie", "jwt",
)
_CRED_VALUE_PATTERNS = (
    re.compile(r"^\s*Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"^eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*$"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_SAFE_LOC_NAMES = frozenset(
    {"method", "path", "headers", "query", "body", "expected_statuses", "candidate_id",
     "sequence", "source", "tags", "estimated_request_count", "auth_profile_id", "metadata"}
)


# --------------------------------------------------------------------------- errors / enums


class RestlerImportError(ValueError):
    """Invalid arguments or a fatal import condition (unreadable/oversized file)."""


class ReplayLogParseError(ValueError):
    """Replay-log text could not be parsed. ``code`` is a stable reason code."""

    def __init__(self, code: str, detail: str, line_no: Optional[int] = None) -> None:
        suffix = f" (line {line_no})" if line_no else ""
        super().__init__(f"{code}: {detail}{suffix}")
        self.code = code
        self.detail = detail
        self.line_no = line_no


class AuthHeaderPolicy(str, Enum):
    REJECT = "reject"
    MAP_TO_PROFILE = "map_to_profile"


@dataclass(frozen=True)
class ImportLimits:
    max_file_bytes: int = 1_000_000
    max_requests_per_sequence: int = 50
    max_body_bytes: int = 65_536

    def __post_init__(self) -> None:
        for name in ("max_file_bytes", "max_requests_per_sequence", "max_body_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise RestlerImportError(f"ImportLimits.{name} must be a positive int")


# --------------------------------------------------------------------------- result records


@dataclass(frozen=True)
class ParsedRequest:
    line_no: int
    method: str
    request_target: str
    headers: tuple[tuple[str, str], ...]  # credential-header values replaced by REDACTED_VALUE
    body_text: Optional[str]  # JSON text with log-level whitespace escapes decoded
    settings: tuple[tuple[str, float], ...]
    recorded_status: Optional[int]


@dataclass(frozen=True)
class ParsedReplayLog:
    source_name: str
    requests: tuple[ParsedRequest, ...]
    ignored_line_count: int  # every line that is not a request, setting or response line


@dataclass(frozen=True)
class Rejection:
    source_name: str
    code: str
    line_no: Optional[int]
    detail: str


@dataclass(frozen=True)
class ImportNote:
    source_name: str
    code: str
    line_no: Optional[int]
    detail: str


@dataclass(frozen=True)
class RequestProvenance:
    index: int  # 0-based position in the sequence
    line_no: int
    recorded_status: Optional[int]
    settings: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class Provenance:
    source_file: str
    requests: tuple[RequestProvenance, ...]
    dropped_auth_header_names: tuple[str, ...]  # names only, never values


@dataclass(frozen=True)
class RestlerImportResult:
    candidates: tuple[CandidateSequence, ...]
    provenance: Mapping[str, Provenance]
    rejections: tuple[Rejection, ...]
    notes: tuple[ImportNote, ...]


# --------------------------------------------------------------------------- classification helpers


def _is_credential_header(lname: str) -> bool:
    if lname in _CRED_HEADER_EXACT or lname == "key" or lname.endswith(("-key", "_key")):
        return True
    return any(s in lname for s in _CRED_HEADER_SUBSTRINGS)


def _is_credential_key(key: str) -> bool:
    norm = re.sub(r"[^a-z0-9]", "", key.lower())
    return norm in {"pwd", "pass"} or any(s in norm for s in _CRED_KEY_SUBSTRINGS)


def _looks_like_credential_value(value: str) -> bool:
    return any(p.search(value) for p in _CRED_VALUE_PATTERNS)


# --------------------------------------------------------------------------- parser


def _has_control_char(line: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in line)


def _decode_body(raw: str, line_no: int) -> str:
    """Decode log-level escapes in a body, JSON-string aware.

    Outside JSON strings the two-character sequences ``\\r``, ``\\n`` and ``\\t``
    are whitespace. Inside strings *no* backslash is accepted: how the log
    escapes backslashes is undocumented, so any guess could change the body.
    """
    out: list[str] = []
    in_string = False
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if ord(c) < 0x20 or ord(c) == 0x7F:
            raise ReplayLogParseError("body_control_character", "control character in body", line_no)
        if in_string:
            if c == "\\":
                raise ReplayLogParseError(
                    "body_string_escape_unsupported",
                    "backslash escape inside a JSON string is ambiguous in the log format",
                    line_no,
                )
            if c == '"':
                in_string = False
            out.append(c)
            i += 1
        elif c == '"':
            in_string = True
            out.append(c)
            i += 1
        elif c == "\\":
            repl = {"r": "\r", "n": "\n", "t": "\t"}.get(raw[i + 1 : i + 2])
            if repl is None:
                raise ReplayLogParseError("body_invalid_escape", "unsupported escape outside a JSON string", line_no)
            out.append(repl)
            i += 2
        else:
            out.append(c)
            i += 1
    if in_string:
        raise ReplayLogParseError("body_unterminated_string", "unterminated JSON string in body", line_no)
    return "".join(out).strip(" \t\r\n")


def _split_header_block(rest: str, line_no: int) -> tuple[list[str], str]:
    headers: list[str] = []
    pos = 0
    while True:
        if rest.startswith(_CRLF, pos):
            return headers, rest[pos + len(_CRLF) :]
        end = rest.find(_CRLF, pos)
        if end == -1:
            raise ReplayLogParseError("header_block_malformed", "header block is not terminated by a blank line", line_no)
        headers.append(rest[pos:end])
        pos = end + len(_CRLF)


def _parse_request_line(line: str, line_no: int) -> tuple[str, str, tuple[tuple[str, str], ...], Optional[str]]:
    match = _REQUEST_RE.match(line)
    if not match:
        raise ReplayLogParseError("request_line_malformed", "request line does not match the documented format", line_no)
    method, target, version, rest = match.groups()
    if version != "HTTP/1.1":
        raise ReplayLogParseError("unsupported_http_version", "only HTTP/1.1 is supported", line_no)
    if method not in SUPPORTED_METHODS:
        raise ReplayLogParseError("unsupported_method", "HTTP method is not supported", line_no)
    header_lines, body_raw = _split_header_block(rest, line_no)
    headers: list[tuple[str, str]] = []
    for header_line in header_lines:
        hm = _HEADER_RE.match(header_line)
        if not hm or "\\" in hm.group(2) or len(hm.group(2)) > _MAX_HEADER_VALUE:
            raise ReplayLogParseError("header_malformed", "malformed header line", line_no)
        name, value = hm.group(1), hm.group(2)
        if _is_credential_header(name.lower()):
            value = REDACTED_VALUE
        headers.append((name, value))
    body_text = _decode_body(body_raw, line_no) if body_raw else ""
    return method, target, tuple(headers), (body_text or None)


@dataclass
class _Draft:
    line_no: int
    method: str
    target: str
    headers: tuple[tuple[str, str], ...]
    body_text: Optional[str]
    settings: dict
    status: Optional[int] = None


def parse_replay_log_text(text: str, *, source_name: str = "<memory>") -> ParsedReplayLog:
    """Parse documented replay-log text. Raises ``ReplayLogParseError``."""
    if not isinstance(text, str):
        raise RestlerImportError("text must be a str")
    if "\x00" in text:
        raise ReplayLogParseError("invalid_characters", "NUL character in log")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    drafts: list[_Draft] = []
    ignored = 0
    for line_no, raw in enumerate(lines, start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        if line.startswith("->"):
            if _has_control_char(line):
                raise ReplayLogParseError("invalid_characters", "control character in request line", line_no)
            method, target, headers, body = _parse_request_line(line, line_no)
            drafts.append(_Draft(line_no, method, target, headers, body, {}))
        elif line.startswith("!"):
            if _has_control_char(line):
                raise ReplayLogParseError("invalid_characters", "control character in setting line", line_no)
            sm = _SETTING_RE.match(line)
            if not sm:
                raise ReplayLogParseError("setting_malformed", "malformed setting line", line_no)
            if sm.group(1) not in SUPPORTED_SETTINGS:
                raise ReplayLogParseError("setting_unsupported", "setting name is not supported", line_no)
            if not drafts:
                raise ReplayLogParseError("setting_without_request", "setting line before any request", line_no)
            if sm.group(1) in drafts[-1].settings:
                raise ReplayLogParseError("setting_duplicate", "setting repeated for one request", line_no)
            drafts[-1].settings[sm.group(1)] = float(sm.group(2))
        elif line.startswith("PREVIOUS RESPONSE:"):
            if _has_control_char(line):
                raise ReplayLogParseError("invalid_characters", "control character in response line", line_no)
            rm = _RESPONSE_RE.match(line)
            if not rm:
                raise ReplayLogParseError("response_line_malformed", "response status could not be parsed", line_no)
            if not drafts:
                raise ReplayLogParseError("response_without_request", "response line before any request", line_no)
            if drafts[-1].status is not None:
                raise ReplayLogParseError("duplicate_response", "more than one response line for a request", line_no)
            drafts[-1].status = int(rm.group(1))
        else:
            ignored += 1

    if not drafts:
        raise ReplayLogParseError("no_requests", "no request lines found")
    requests = tuple(
        ParsedRequest(d.line_no, d.method, d.target, d.headers, d.body_text, tuple(d.settings.items()), d.status)
        for d in drafts
    )
    return ParsedReplayLog(source_name=source_name, requests=requests, ignored_line_count=ignored)


# --------------------------------------------------------------------------- import


class _Reject(Exception):
    def __init__(self, code: str, detail: str, line_no: Optional[int] = None) -> None:
        super().__init__(code)
        self.code, self.detail, self.line_no = code, detail, line_no


class _DuplicateKeyError(Exception):
    pass


@dataclass(frozen=True)
class _TargetContext:
    base: str
    scheme: str
    host: str
    port: int


def _make_target_context(target: TargetConfig) -> _TargetContext:
    try:
        check_target_url(target.base_url, target.allowed_hosts)
    except TargetNotAllowedError:
        raise RestlerImportError("target.base_url is not an authorized target") from None
    except ValueError:
        raise RestlerImportError("target.allowed_hosts contains an invalid entry") from None
    parts = urlsplit(target.base_url)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise RestlerImportError(
            "target.base_url must not contain a path, query or fragment (join semantics unverified)"
        )
    scheme = parts.scheme.lower()
    return _TargetContext(
        base=f"{scheme}://{parts.netloc}",
        scheme=scheme,
        host=(parts.hostname or "").rstrip("."),
        port=parts.port or _DEFAULT_PORTS[scheme],
    )


def _validate_arguments(paths, target, auth_policy, auth_profile_id, limits) -> list[Path]:
    if isinstance(paths, (str, bytes)) or not isinstance(paths, Sequence):
        raise RestlerImportError("paths must be a sequence of pathlib.Path objects")
    if not paths:
        raise RestlerImportError("paths must not be empty")
    if any(not isinstance(p, Path) for p in paths):
        raise RestlerImportError("paths must be a sequence of pathlib.Path objects")
    if not isinstance(target, TargetConfig):
        raise RestlerImportError("target must be a TargetConfig")
    if not isinstance(auth_policy, AuthHeaderPolicy):
        raise RestlerImportError("auth_policy must be an AuthHeaderPolicy")
    if not isinstance(limits, ImportLimits):
        raise RestlerImportError("limits must be an ImportLimits")
    if auth_policy is AuthHeaderPolicy.REJECT:
        if auth_profile_id is not None:
            raise RestlerImportError("auth_profile_id is only valid with AuthHeaderPolicy.MAP_TO_PROFILE")
    else:
        if not isinstance(auth_profile_id, str) or not auth_profile_id:
            raise RestlerImportError("MAP_TO_PROFILE requires auth_profile_id")
        if target.auth_profile_id is None or auth_profile_id != target.auth_profile_id:
            raise RestlerImportError("auth_profile_id must equal target.auth_profile_id")
    return [Path(p) for p in sorted({os.fspath(p) for p in paths})]


def _read_input_file(path: Path, limits: ImportLimits) -> bytes:
    try:
        with open(path, "rb") as handle:
            data = handle.read(limits.max_file_bytes + 1)
    except OSError as exc:
        raise RestlerImportError(f"cannot read input file {path.name} ({type(exc).__name__})") from None
    if len(data) > limits.max_file_bytes:
        raise RestlerImportError(f"input file {path.name} exceeds max_file_bytes")
    return data


def _split_target(target: str, line_no: int) -> tuple[str, Optional[str]]:
    if "#" in target:
        raise _Reject("url_fragment_not_allowed", "request target contains a URL fragment", line_no)
    path, sep, query = target.partition("?")
    if path.startswith("//") or not _PATH_RE.fullmatch(path):
        raise _Reject("request_target_invalid", "request target is not a supported origin-form path", line_no)
    return path, (query if sep else None)


def _parse_query(query: Optional[str], line_no: int) -> dict[str, str]:
    result: dict[str, str] = {}
    if query is None:
        return result
    for part in query.split("&"):
        key, eq, value = part.partition("=")
        if not key or not eq:
            raise _Reject("query_malformed", "query parameters must be non-empty key=value pairs", line_no)
        if not _QUERY_PART_RE.fullmatch(key) or not _QUERY_PART_RE.fullmatch(value):
            raise _Reject(
                "query_unsupported_characters",
                "query uses characters outside the supported unreserved set",
                line_no,
            )
        if _is_credential_key(key) or _looks_like_credential_value(value):
            raise _Reject("query_credential_field", "query contains a credential-like parameter", line_no)
        if key in result:
            raise _Reject("query_duplicate_key", "query contains a duplicate key", line_no)
        result[key] = value
    return result


def _check_host_header(value: str, ctx: _TargetContext, line_no: int) -> None:
    if not value or any(c in value for c in "/@\\ "):
        raise _Reject("host_header_invalid", "Host header is malformed", line_no)
    try:
        parts = urlsplit("//" + value)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise _Reject("host_header_invalid", "Host header is malformed", line_no) from None
    if not host:
        raise _Reject("host_header_invalid", "Host header is malformed", line_no)
    effective = port if port is not None else _DEFAULT_PORTS[ctx.scheme]
    if host.rstrip(".") != ctx.host or effective != ctx.port:
        raise _Reject("host_mismatch", "Host header does not match the configured target", line_no)


def _pairs_no_dupes(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError()
        result[key] = value
    return result


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


def _reject_constant(name: str) -> Any:
    raise ValueError("non-finite constant")


def _scan_json_value(value: Any, depth: int, line_no: int) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise _Reject("body_too_deep", "body nesting is too deep", line_no)
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_credential_key(key):
                raise _Reject("body_credential_field", "body contains a credential-like field name", line_no)
            _scan_json_value(item, depth + 1, line_no)
    elif isinstance(value, list):
        for item in value:
            _scan_json_value(item, depth + 1, line_no)
    elif isinstance(value, str) and _looks_like_credential_value(value):
        raise _Reject("body_credential_field", "body contains a credential-like value", line_no)


def _decode_json_body(text: str, content_type: Optional[str], line_no: int) -> Any:
    if content_type is None:
        raise _Reject("body_content_type_missing", "request has a body but no Content-Type", line_no)
    media = content_type.split(";", 1)[0].strip().lower()
    if media != "application/json" and not (media.startswith("application/") and media.endswith("+json")):
        raise _Reject("body_unsupported_content_type", "only JSON bodies are supported", line_no)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_no_dupes,
            parse_float=_parse_float,
            parse_constant=_reject_constant,
        )
    except _DuplicateKeyError:
        raise _Reject("body_duplicate_key", "body contains a duplicate JSON key", line_no) from None
    except RecursionError:
        raise _Reject("body_too_deep", "body nesting is too deep", line_no) from None
    except ValueError:
        raise _Reject("body_not_json", "body is not valid JSON", line_no) from None
    if not isinstance(value, (dict, list)):
        raise _Reject("body_unsupported_type", "only JSON objects and arrays are supported", line_no)
    _scan_json_value(value, 0, line_no)
    return value


def _safe_validation_detail(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(
            str(p) if isinstance(p, int) or p in _SAFE_LOC_NAMES else "*" for p in err.get("loc", ())
        )
        parts.append(f"{loc}:{err.get('type', 'invalid')}")
    return "model validation failed: " + ", ".join(parts)


def _build_request(
    preq: ParsedRequest,
    ctx: _TargetContext,
    target: TargetConfig,
    auth_policy: AuthHeaderPolicy,
    limits: ImportLimits,
    dropped: set[str],
) -> tuple[CandidateRequest, dict]:
    line = preq.line_no
    if preq.body_text is not None and len(preq.body_text.encode("utf-8")) > limits.max_body_bytes:
        raise _Reject("body_too_large", f"request body exceeds {limits.max_body_bytes} bytes", line)
    path, query_text = _split_target(preq.request_target, line)

    seen: set[str] = set()
    kept: dict[str, str] = {}
    host_value: Optional[str] = None
    for name, value in preq.headers:
        lname = name.lower()
        if lname in seen:
            raise _Reject("duplicate_header", "request repeats a header name", line)
        seen.add(lname)
        if lname == "host":
            host_value = value
        elif _is_credential_header(lname):
            if auth_policy is AuthHeaderPolicy.REJECT:
                raise _Reject("auth_material_present", f"credential-bearing header present: {lname}", line)
            dropped.add(lname)
        elif lname in _ALLOWED_HEADERS:
            kept[_ALLOWED_HEADERS[lname]] = value
        else:
            raise _Reject("unsupported_header", f"unsupported header: {lname}", line)
    if host_value is None:
        raise _Reject("host_header_missing", "request has no Host header", line)
    _check_host_header(host_value, ctx, line)

    try:
        check_target_url(f"{ctx.base}{preq.request_target}", target.allowed_hosts)
    except (TargetNotAllowedError, ValueError):
        raise _Reject("target_not_allowed", "request is not an authorized target", line) from None

    query = _parse_query(query_text, line)
    body = None
    if preq.body_text is not None:
        body = _decode_json_body(preq.body_text, kept.get("Content-Type"), line)

    try:
        request = CandidateRequest(
            method=preq.method, path=path, headers=dict(kept), query=dict(query),
            body=body, expected_statuses=[],
        )
    except ValidationError as exc:
        raise _Reject("candidate_validation_failed", _safe_validation_detail(exc), line) from None
    canon = {"method": preq.method, "path": path, "headers": kept, "query": query, "body": body}
    return request, canon


def _build_candidate(
    parsed: ParsedReplayLog,
    ctx: _TargetContext,
    target: TargetConfig,
    auth_policy: AuthHeaderPolicy,
    auth_profile_id: Optional[str],
    limits: ImportLimits,
) -> tuple[CandidateSequence, str, str, Provenance, list[ImportNote]]:
    source = parsed.source_name
    if len(parsed.requests) > limits.max_requests_per_sequence:
        raise _Reject(
            "too_many_requests",
            f"sequence has {len(parsed.requests)} requests; limit is {limits.max_requests_per_sequence}",
        )
    dropped: set[str] = set()
    requests: list[CandidateRequest] = []
    canon_requests: list[dict] = []
    for preq in parsed.requests:
        request, canon = _build_request(preq, ctx, target, auth_policy, limits, dropped)
        requests.append(request)
        canon_requests.append(canon)

    sequence_profile = auth_profile_id if dropped else None
    canonical = json.dumps(
        {"auth_profile_id": sequence_profile, "sequence": canon_requests},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    candidate_id = "imp-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    try:
        candidate = CandidateSequence(
            candidate_id=candidate_id, sequence=requests, source="imported", tags=[],
            estimated_request_count=len(requests), auth_profile_id=sequence_profile, metadata={},
        )
    except ValidationError as exc:
        raise _Reject("candidate_validation_failed", _safe_validation_detail(exc)) from None

    provenance = Provenance(
        source_file=source,
        requests=tuple(
            RequestProvenance(i, p.line_no, p.recorded_status, p.settings)
            for i, p in enumerate(parsed.requests)
        ),
        dropped_auth_header_names=tuple(sorted(dropped)),
    )
    notes: list[ImportNote] = []
    if dropped:
        notes.append(ImportNote(source, "auth_replaced_by_profile", None,
                                "credential-bearing headers were dropped and replaced by the configured auth "
                                "profile; the profile's identity may differ from the recorded run"))
    for p in parsed.requests:
        if p.recorded_status in (401, 403):
            notes.append(ImportNote(source, "likely_requires_auth", p.line_no,
                                    f"recorded response status was {p.recorded_status}"))
    if any(p.settings for p in parsed.requests):
        notes.append(ImportNote(source, "settings_not_applied", None,
                                "recorded timing settings are provenance only and are not applied"))
    return candidate, candidate_id, canonical, provenance, notes


def import_replay_logs(
    paths: Sequence[Path],
    *,
    target: TargetConfig,
    auth_policy: AuthHeaderPolicy = AuthHeaderPolicy.REJECT,
    auth_profile_id: Optional[str] = None,
    limits: ImportLimits = ImportLimits(),
) -> RestlerImportResult:
    """Import replay-log files (one candidate sequence per file). Read-only."""
    ordered = _validate_arguments(paths, target, auth_policy, auth_profile_id, limits)
    ctx = _make_target_context(target)

    candidates: list[CandidateSequence] = []
    provenance: dict[str, Provenance] = {}
    rejections: list[Rejection] = []
    notes: list[ImportNote] = []
    seen: dict[str, tuple[str, str]] = {}  # candidate_id -> (canonical, first source)

    for path in ordered:
        source = str(path)
        data = _read_input_file(path, limits)  # fatal on unreadable / oversized
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            rejections.append(Rejection(source, "file_not_utf8", None, "input file is not valid UTF-8"))
            continue
        try:
            parsed = parse_replay_log_text(text, source_name=source)
            candidate, cid, canonical, prov, file_notes = _build_candidate(
                parsed, ctx, target, auth_policy, auth_profile_id, limits
            )
        except ReplayLogParseError as exc:
            rejections.append(Rejection(source, exc.code, exc.line_no, exc.detail))
            continue
        except _Reject as exc:
            rejections.append(Rejection(source, exc.code, exc.line_no, exc.detail))
            continue
        if cid in seen:
            same = seen[cid][0] == canonical
            rejections.append(Rejection(
                source,
                "duplicate_sequence" if same else "candidate_id_collision",
                None,
                f"same candidate id as the sequence from {seen[cid][1]}",
            ))
            continue
        seen[cid] = (canonical, source)
        candidates.append(candidate)
        provenance[cid] = prov
        notes.extend(file_notes)

    return RestlerImportResult(
        candidates=tuple(candidates),
        provenance=MappingProxyType(provenance),
        rejections=tuple(rejections),
        notes=tuple(notes),
    )
