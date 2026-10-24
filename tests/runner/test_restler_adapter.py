"""Tests for the offline RESTler replay-log importer (Milestone 2a, experimental).

All fixtures are SYNTHETIC: hand-written from RESTler's documented replay-log
format, not captured from a real RESTler run. Passing these tests does not show
compatibility with real RESTler output, nor any fuzzing-effectiveness gain.
"""

from __future__ import annotations

import dataclasses
import json
import socket
import subprocess
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from adaptive_restler.models import CandidateSequence, TargetConfig
from adaptive_restler.runner import restler_adapter as ra
from adaptive_restler.runner.restler_adapter import (
    REASON_CODES,
    REDACTED_VALUE,
    AuthHeaderPolicy,
    ImportLimits,
    ReplayLogParseError,
    RestlerImportError,
    import_replay_logs,
    parse_replay_log_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "restler"
TWO_STEP = FIXTURES / "synthetic_two_step.replay.txt"
GET_QUERY = FIXTURES / "synthetic_get_with_query.replay.txt"
AUTH_FIXTURE = FIXTURES / "synthetic_auth_header.replay.txt"
SECRET = "SYNTHETIC-SECRET-VALUE-123"
CRLF = "\\r\\n"  # literal backslash-r-backslash-n, as written in replay logs
PROFILE = "test-profile-1"
GOLDEN_TWO_STEP_ID = "imp-5f27dea3a6183d6539ab"
GOLDEN_GET_QUERY_ID = "imp-9b0683b7fa3c19ba006d"
HEADERS = (
    ("Accept", "application/json"),
    ("Host", "localhost:8888"),
    ("Content-Type", "application/json"),
)


# ------------------------------------------------------------------ helpers


def target(base_url="http://localhost:8888", allowed=("localhost",), profile=None):
    return TargetConfig(base_url=base_url, allowed_hosts=list(allowed), auth_profile_id=profile)


def req(method="GET", tgt="/api/x", headers=HEADERS, body=None, version="HTTP/1.1"):
    head = "".join(f"{n}: {v}{CRLF}" for n, v in headers)
    line = f"-> {method} {tgt} {version}{CRLF}{head}{CRLF}"
    return line + (body + CRLF if body is not None else "")


def resp(status=200):
    return f"PREVIOUS RESPONSE: 'HTTP/1.1 {status} OK{CRLF}{CRLF}'"


def log(*lines):
    return "\n".join(lines) + "\n"


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def run(tmp_path, text, **kwargs):
    kwargs.setdefault("target", target())
    return import_replay_logs([write(tmp_path, "case.replay.txt", text)], **kwargs)


def only_rejection(result):
    assert not result.candidates
    assert len(result.rejections) == 1
    rejection = result.rejections[0]
    assert rejection.code in REASON_CODES
    assert SECRET not in repr(rejection)
    return rejection


def snapshot(root):
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))}


# ------------------------------------------------------------------ parser


def test_parser_is_labelled_experimental():
    assert ra.PARSER_STATUS == "experimental"


def test_parse_documented_two_step_fixture():
    parsed = parse_replay_log_text(TWO_STEP.read_text(encoding="utf-8"), source_name="two-step")
    assert parsed.source_name == "two-step"
    first, second = parsed.requests
    assert (first.method, first.request_target) == ("POST", "/api/blog/posts")
    assert (second.method, second.request_target) == ("PUT", "/api/blog/posts/5875")
    assert first.headers == HEADERS
    assert json.loads(first.body_text) == {"id": 0, "body": "fuzzstring"}
    assert first.settings == (("producer_timing_delay", 0.0), ("max_async_wait_time", 0.0))
    assert (first.recorded_status, second.recorded_status) == (201, 500)
    assert parsed.ignored_line_count == 3  # two comment lines + one blank line


def test_blank_lines_do_not_split_a_sequence():
    parsed = parse_replay_log_text(log(req(), "", "", req(method="POST", body='{"a":1}')))
    assert [r.method for r in parsed.requests] == ["GET", "POST"]


def test_unrelated_lines_are_ignored_and_counted():
    parsed = parse_replay_log_text(log("hello", "", req(), "# note", resp(204)))
    assert len(parsed.requests) == 1
    assert parsed.ignored_line_count == 3
    assert parsed.requests[0].recorded_status == 204


def test_escaped_whitespace_outside_json_strings_is_decoded():
    parsed = parse_replay_log_text(log(req(method="POST", body='{\\n"a":\\t1}')))
    assert json.loads(parsed.requests[0].body_text) == {"a": 1}


def test_crlf_text_inside_a_json_string_is_not_a_delimiter_and_is_rejected_as_ambiguous():
    with pytest.raises(ReplayLogParseError) as info:
        parse_replay_log_text(log(req(method="POST", body='{"a":"x' + CRLF + 'y"}')))
    assert info.value.code == "body_string_escape_unsupported"
    assert info.value.line_no == 1


@pytest.mark.parametrize(
    "text,code",
    [
        ("", "no_requests"),
        ("just some text\n", "no_requests"),
        (log(req()).replace("-> GET", "-> get"), "request_line_malformed"),
        (log(req(version="HTTP/2.0")), "unsupported_http_version"),
        (log(req(method="TRACE")), "unsupported_method"),
        (f"-> GET /x HTTP/1.1{CRLF}Host: a{CRLF}\n", "header_block_malformed"),
        (log(req()).replace("Host: localhost:8888", "Host localhost:8888"), "header_malformed"),
        (log(req(), "! producer_timing_delay"), "setting_malformed"),
        (log(req(), "! made_up_setting 1"), "setting_unsupported"),
        (log(req(), "! producer_timing_delay 1", "! producer_timing_delay 2"), "setting_duplicate"),
        (log("! producer_timing_delay 1", req()), "setting_without_request"),
        (log(req(), "PREVIOUS RESPONSE: 'garbage'"), "response_line_malformed"),
        (log(req(), f"PREVIOUS RESPONSE: 'HTTP/1.1 999 X{CRLF}'"), "response_line_malformed"),
        (log(resp(200), req()), "response_without_request"),
        (log(req(), resp(200), resp(201)), "duplicate_response"),
        ("-> GET /x HTTP/1.1\x00\n", "invalid_characters"),
        (log(req(method="POST", body='{"a":"unterminated}')[: -len(CRLF)]), "body_unterminated_string"),
        (log(req(method="POST", body='{"a":\\x}')), "body_invalid_escape"),
    ],
)
def test_malformed_logs_raise_stable_codes(text, code):
    with pytest.raises(ReplayLogParseError) as info:
        parse_replay_log_text(text)
    assert info.value.code == code
    assert code in REASON_CODES


def test_parse_error_reports_line_number():
    with pytest.raises(ReplayLogParseError) as info:
        parse_replay_log_text(log("a", "b", "-> nope"))
    assert (info.value.code, info.value.line_no) == ("request_line_malformed", 3)


def test_parse_redacts_credential_header_values():
    parsed = parse_replay_log_text(AUTH_FIXTURE.read_text(encoding="utf-8"))
    assert SECRET not in repr(parsed)
    assert ("Authorization", REDACTED_VALUE) in parsed.requests[0].headers


def test_parse_rejects_non_string_input():
    with pytest.raises(RestlerImportError):
        parse_replay_log_text(b"-> GET /x")


# ------------------------------------------------------------------ import: happy path


def test_import_two_step_fixture():
    result = import_replay_logs([TWO_STEP], target=target())
    assert not result.rejections
    (candidate,) = result.candidates
    assert isinstance(candidate, CandidateSequence)
    assert candidate.source == "imported"
    assert candidate.estimated_request_count == 2
    assert candidate.auth_profile_id is None
    assert candidate.tags == [] and candidate.metadata == {}
    assert [r.method for r in candidate.sequence] == ["POST", "PUT"]
    assert all(r.expected_statuses == [] for r in candidate.sequence)
    assert candidate.sequence[0].headers == {"Accept": "application/json", "Content-Type": "application/json"}
    assert candidate.sequence[0].body == {"id": 0, "body": "fuzzstring"}
    prov = result.provenance[candidate.candidate_id]
    assert [(p.index, p.recorded_status) for p in prov.requests] == [(0, 201), (1, 500)]
    assert prov.requests[0].settings == (("producer_timing_delay", 0.0), ("max_async_wait_time", 0.0))
    assert prov.dropped_auth_header_names == ()
    assert [n.code for n in result.notes] == ["settings_not_applied"]


def test_import_get_with_query_fixture():
    result = import_replay_logs([GET_QUERY], target=target())
    (candidate,) = result.candidates
    request = candidate.sequence[0]
    assert (request.method, request.path, request.query, request.body) == (
        "GET", "/api/items", {"page": "2", "limit": "10"}, None,
    )
    assert request.headers == {"Accept": "application/json"}
    assert not result.notes and not result.rejections


def test_empty_body_after_escaped_whitespace_is_no_body(tmp_path):
    result = run(tmp_path, log(req(method="POST", body="")))
    assert result.candidates[0].sequence[0].body is None


def test_non_credential_field_named_author_is_accepted(tmp_path):
    result = run(tmp_path, log(req(method="POST", body='{"author":"x"}')))
    assert result.candidates and not result.rejections


# ------------------------------------------------------------------ determinism / ids


def test_candidate_ids_are_stable_content_hashes():
    result = import_replay_logs([TWO_STEP, GET_QUERY], target=target())
    assert [c.candidate_id for c in result.candidates] == [GOLDEN_GET_QUERY_ID, GOLDEN_TWO_STEP_ID]


def test_input_order_does_not_change_output():
    forward = import_replay_logs([GET_QUERY, TWO_STEP], target=target())
    backward = import_replay_logs([TWO_STEP, GET_QUERY], target=target())
    assert [c.candidate_id for c in forward.candidates] == [c.candidate_id for c in backward.candidates]
    assert forward.candidates == backward.candidates


def test_identical_content_is_rejected_as_duplicate_first_wins(tmp_path):
    a = write(tmp_path, "a.replay.txt", log(req(), resp(200)))
    b = write(tmp_path, "b.replay.txt", log("# other comment", req(), resp(404)))
    result = import_replay_logs([b, a], target=target())
    assert len(result.candidates) == 1
    assert result.provenance[result.candidates[0].candidate_id].source_file == str(a)
    assert [(r.source_name, r.code) for r in result.rejections] == [(str(b), "duplicate_sequence")]


def test_id_collision_with_different_content_is_rejected(tmp_path, monkeypatch):
    real = ra.hashlib.sha256

    class Fixed:
        def __init__(self, data):
            self._real = real(data)

        def hexdigest(self):
            return "0" * 64

    monkeypatch.setattr(ra.hashlib, "sha256", Fixed)
    a = write(tmp_path, "a.replay.txt", log(req(tgt="/api/one")))
    b = write(tmp_path, "b.replay.txt", log(req(tgt="/api/two")))
    result = import_replay_logs([a, b], target=target())
    assert len(result.candidates) == 1
    assert result.rejections[0].code == "candidate_id_collision"


# ------------------------------------------------------------------ targets, hosts, request targets


@pytest.mark.parametrize(
    "host,code",
    [
        ("evil.example:8888", "host_mismatch"),
        ("localhost:9999", "host_mismatch"),
        ("localhost", "host_mismatch"),
        ("", "host_header_invalid"),
        ("localhost:8888/x", "host_header_invalid"),
        ("user@localhost:8888", "host_header_invalid"),
        ("localhost:notaport", "host_header_invalid"),
    ],
)
def test_host_header_must_match_the_configured_target(tmp_path, host, code):
    headers = (("Accept", "application/json"), ("Host", host))
    rejection = only_rejection(run(tmp_path, log(req(headers=headers))))
    assert rejection.code == code


def test_missing_host_header_is_rejected(tmp_path):
    rejection = only_rejection(run(tmp_path, log(req(headers=(("Accept", "application/json"),)))))
    assert rejection.code == "host_header_missing"


def test_host_header_is_case_insensitive_and_dropped(tmp_path):
    headers = (("Accept", "application/json"), ("Host", "LOCALHOST:8888"))
    result = run(tmp_path, log(req(headers=headers)))
    assert result.candidates[0].sequence[0].headers == {"Accept": "application/json"}


@pytest.mark.parametrize(
    "base,allowed,host,ok",
    [
        ("http://localhost", ("localhost",), "localhost", True),
        ("http://localhost", ("localhost",), "localhost:80", True),
        ("http://localhost", ("localhost",), "localhost:8080", False),
        ("https://localhost", ("localhost",), "localhost:443", True),
        ("https://localhost", ("localhost",), "localhost:80", False),
        ("http://localhost:8888", ("localhost",), "localhost:8888", True),
    ],
)
def test_effective_port_semantics(tmp_path, base, allowed, host, ok):
    headers = (("Accept", "application/json"), ("Host", host))
    result = run(tmp_path, log(req(headers=headers)), target=target(base, allowed))
    assert bool(result.candidates) is ok
    if not ok:
        assert result.rejections[0].code == "host_mismatch"


def test_unauthorized_base_url_is_fatal(tmp_path):
    for allowed in (["localhost:9999"], ["other.example"]):
        bad = TargetConfig.model_construct(base_url="http://localhost:8888", allowed_hosts=allowed)
        with pytest.raises(RestlerImportError):
            run(tmp_path, log(req()), target=bad)


def test_invalid_allowlist_entry_is_fatal(tmp_path):
    bad = TargetConfig.model_construct(base_url="http://localhost:8888", allowed_hosts=["bad/host"])
    with pytest.raises(RestlerImportError):
        run(tmp_path, log(req()), target=bad)


def test_base_url_with_path_prefix_is_fatal(tmp_path):
    with pytest.raises(RestlerImportError):
        run(tmp_path, log(req()), target=target("http://localhost:8888/api"))


def test_per_request_guard_failure_is_a_rejection(tmp_path, monkeypatch):
    real = ra.check_target_url

    def fake(url, hosts, *args, **kwargs):
        if url.endswith("/api/x"):
            raise ra.TargetNotAllowedError("nope")
        return real(url, hosts, *args, **kwargs)

    monkeypatch.setattr(ra, "check_target_url", fake)
    assert only_rejection(run(tmp_path, log(req()))).code == "target_not_allowed"


@pytest.mark.parametrize(
    "tgt,code",
    [
        ("http://evil.example/x", "request_target_invalid"),
        ("//evil.example/x", "request_target_invalid"),
        ("api/x", "request_target_invalid"),
        ("/api\\x", "request_target_invalid"),
        ("/api/\u00e9", "request_target_invalid"),
        ("/api/%41", "request_target_invalid"),
        ("/api/x#frag", "url_fragment_not_allowed"),
        ("/api/x?a=1#frag", "url_fragment_not_allowed"),
        ("/api/x?a=1&a=2", "query_duplicate_key"),
        ("/api/x?a=1&&b=2", "query_malformed"),
        ("/api/x?a", "query_malformed"),
        ("/api/x?", "query_malformed"),
        ("/api/x?a=b+c", "query_unsupported_characters"),
        ("/api/x?a=%41", "query_unsupported_characters"),
        ("/api/x?a=b=c", "query_unsupported_characters"),
        ("/api/x?access_token=abc123", "query_credential_field"),
        ("/api/x?note=eyJabcde.fghijk.lmnop", "query_credential_field"),
    ],
)
def test_bad_request_targets_are_rejected(tmp_path, tgt, code):
    assert only_rejection(run(tmp_path, log(req(tgt=tgt)))).code == code


# ------------------------------------------------------------------ headers and authentication


@pytest.mark.parametrize(
    "name", ["Authorization", "Cookie", "X-Api-Key", "X-Auth-Token", "Ocp-Apim-Subscription-Key"]
)
def test_credential_headers_are_rejected_by_default(tmp_path, name):
    headers = HEADERS + ((name, SECRET),)
    result = run(tmp_path, log(req(headers=headers)))
    assert only_rejection(result).code == "auth_material_present"
    assert SECRET not in repr(result)


def test_auth_fixture_is_rejected_without_leaking_the_secret():
    result = import_replay_logs([AUTH_FIXTURE], target=target())
    assert result.rejections[0].code == "auth_material_present"
    assert SECRET not in repr(result)


def test_map_to_profile_drops_values_and_records_names_only():
    result = import_replay_logs(
        [AUTH_FIXTURE],
        target=target(profile=PROFILE),
        auth_policy=AuthHeaderPolicy.MAP_TO_PROFILE,
        auth_profile_id=PROFILE,
    )
    (candidate,) = result.candidates
    assert candidate.auth_profile_id == PROFILE
    assert candidate.sequence[0].headers == {"Accept": "application/json"}
    prov = result.provenance[candidate.candidate_id]
    assert prov.dropped_auth_header_names == ("authorization",)
    assert {n.code for n in result.notes} >= {"auth_replaced_by_profile"}
    assert SECRET not in repr(result)


def test_map_to_profile_does_not_attach_a_profile_to_unauthenticated_sequences():
    result = import_replay_logs(
        [GET_QUERY],
        target=target(profile=PROFILE),
        auth_policy=AuthHeaderPolicy.MAP_TO_PROFILE,
        auth_profile_id=PROFILE,
    )
    assert result.candidates[0].auth_profile_id is None


def test_recorded_401_adds_a_likely_requires_auth_note(tmp_path):
    result = run(tmp_path, log(req(), resp(401)))
    assert [n.code for n in result.notes] == ["likely_requires_auth"]
    assert result.candidates  # a note, not a rejection


def test_invalid_auth_arguments_are_fatal(tmp_path):
    path = write(tmp_path, "a.replay.txt", log(req()))
    map_policy, reject_policy = AuthHeaderPolicy.MAP_TO_PROFILE, AuthHeaderPolicy.REJECT
    cases = [
        dict(target=target(), auth_policy=map_policy, auth_profile_id=PROFILE),
        dict(target=target(profile=PROFILE), auth_policy=map_policy, auth_profile_id=None),
        dict(target=target(profile=PROFILE), auth_policy=map_policy, auth_profile_id="other-profile"),
        dict(target=target(profile=PROFILE), auth_policy=reject_policy, auth_profile_id=PROFILE),
        dict(target=target(), auth_policy="reject"),
    ]
    for kwargs in cases:
        with pytest.raises(RestlerImportError):
            import_replay_logs([path], **kwargs)


@pytest.mark.parametrize(
    "extra,code",
    [
        ((("X-Custom", "1"),), "unsupported_header"),
        ((("Content-Length", "2"),), "unsupported_header"),
        ((("Accept", "text/plain"),), "duplicate_header"),
    ],
)
def test_unsupported_and_duplicate_headers_are_rejected(tmp_path, extra, code):
    assert only_rejection(run(tmp_path, log(req(headers=HEADERS + extra)))).code == code


# ------------------------------------------------------------------ bodies


NO_CT = (("Accept", "application/json"), ("Host", "localhost:8888"))


@pytest.mark.parametrize(
    "body,headers,code",
    [
        ('{"a":1}', NO_CT, "body_content_type_missing"),
        ('{"a":1}', NO_CT + (("Content-Type", "text/plain"),), "body_unsupported_content_type"),
        ('{"a":', HEADERS, "body_not_json"),
        ('{"a":1,"a":2}', HEADERS, "body_duplicate_key"),
        ("42", HEADERS, "body_unsupported_type"),
        ('{"a":NaN}', HEADERS, "body_not_json"),
        ('{"a":1e999}', HEADERS, "body_not_json"),
        ('{"password":"hunter2"}', HEADERS, "body_credential_field"),
        ('{"user":{"api_key":"abc"}}', HEADERS, "body_credential_field"),
        ('{"x":["eyJabcde.fghijk.lmnop"]}', HEADERS, "body_credential_field"),
        ("[" * 40 + "]" * 40, HEADERS, "body_too_deep"),
        ("[" * 5000 + "]" * 5000, HEADERS, "body_too_deep"),
    ],
)
def test_bad_bodies_are_rejected(tmp_path, body, headers, code):
    result = run(tmp_path, log(req(method="POST", headers=headers, body=body)))
    rejection = only_rejection(result)
    assert rejection.code == code
    assert "hunter2" not in repr(result)


# ------------------------------------------------------------------ limits and fatal conditions


def test_default_limits():
    assert ImportLimits() == ImportLimits(1_000_000, 50, 65_536)


@pytest.mark.parametrize("bad", [0, -1, True, "5", 1.5])
def test_invalid_limits_are_rejected(bad):
    for field in ("max_file_bytes", "max_requests_per_sequence", "max_body_bytes"):
        with pytest.raises(RestlerImportError):
            ImportLimits(**{field: bad})


def test_oversized_file_is_fatal():
    with pytest.raises(RestlerImportError):
        import_replay_logs([TWO_STEP], target=target(), limits=ImportLimits(max_file_bytes=50))


def test_too_many_requests_is_a_rejection():
    result = import_replay_logs([TWO_STEP], target=target(), limits=ImportLimits(max_requests_per_sequence=1))
    assert result.rejections[0].code == "too_many_requests"


def test_oversized_body_is_a_rejection(tmp_path):
    result = run(tmp_path, log(req(method="POST", body='{"a":"xxxxxxxx"}')), limits=ImportLimits(max_body_bytes=5))
    assert only_rejection(result).code == "body_too_large"


def test_unreadable_inputs_are_fatal(tmp_path):
    for bad in (tmp_path / "missing.replay.txt", tmp_path):
        with pytest.raises(RestlerImportError):
            import_replay_logs([bad], target=target())


def test_non_utf8_file_is_a_rejection(tmp_path):
    path = tmp_path / "bin.replay.txt"
    path.write_bytes(b"\xff\xfe\x00-> GET")
    result = import_replay_logs([path], target=target())
    assert only_rejection(result).code == "file_not_utf8"


@pytest.mark.parametrize("bad_paths", [[], "a.txt", [Path("a"), "b"], None])
def test_invalid_paths_argument_is_fatal(bad_paths):
    with pytest.raises(RestlerImportError):
        import_replay_logs(bad_paths, target=target())


def test_non_target_config_is_fatal():
    with pytest.raises(RestlerImportError):
        import_replay_logs([TWO_STEP], target="http://localhost:8888")


def test_bad_files_do_not_stop_good_files(tmp_path):
    bad = write(tmp_path, "a_bad.replay.txt", "nothing useful\n")
    result = import_replay_logs([bad, TWO_STEP], target=target())
    assert len(result.candidates) == 1
    assert [r.code for r in result.rejections] == ["no_requests"]


# ------------------------------------------------------------------ safe messages


def test_model_validation_failures_do_not_echo_input(tmp_path, monkeypatch):
    def boom(**kwargs):
        raise ValidationError.from_exception_data(
            "CandidateRequest", [{"type": "missing", "loc": ("path",), "input": "LEAKED-VALUE"}]
        )

    monkeypatch.setattr(ra, "CandidateRequest", boom)
    rejection = only_rejection(run(tmp_path, log(req())))
    assert rejection.code == "candidate_validation_failed"
    assert "LEAKED-VALUE" not in rejection.detail
    assert "path" in rejection.detail


# ------------------------------------------------------------------ side effects and immutability


def test_import_has_no_network_subprocess_sleep_or_writes(tmp_path, monkeypatch):
    slow = log(req(), "! producer_timing_delay 3600", "! max_async_wait_time 3600", resp(200))
    inputs = [write(tmp_path, "slow.replay.txt", slow), TWO_STEP, AUTH_FIXTURE]

    def forbidden(*args, **kwargs):
        raise AssertionError("forbidden side effect")

    for obj, name in [
        (socket, "socket"), (socket, "create_connection"), (socket, "getaddrinfo"),
        (subprocess, "Popen"), (time, "sleep"),
    ]:
        monkeypatch.setattr(obj, name, forbidden)
    before = snapshot(tmp_path)
    result = import_replay_logs(inputs, target=target())
    parse_replay_log_text(slow)
    assert snapshot(tmp_path) == before
    assert len(result.candidates) == 2


def test_results_are_immutable():
    result = import_replay_logs([TWO_STEP], target=target())
    assert isinstance(result.candidates, tuple)
    assert isinstance(result.rejections, tuple) and isinstance(result.notes, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.candidates = ()
    with pytest.raises(TypeError):
        result.provenance["x"] = None
    prov = next(iter(result.provenance.values()))
    assert isinstance(prov.requests, tuple) and isinstance(prov.dropped_auth_header_names, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        prov.source_file = "x"
