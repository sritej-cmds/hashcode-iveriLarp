import json

import pytest

from adaptive_restler.runner.artifacts import (
    REDACTED,
    artifact_dir,
    redact,
    write_json_artifact,
)


def test_artifact_dir_layout(tmp_path):
    assert artifact_dir(tmp_path, "run1", "cand-7") == tmp_path / "run1" / "cand-7"
    assert artifact_dir(tmp_path, "run1") == tmp_path / "run1"


@pytest.mark.parametrize("bad", ["", "..", "../x", "a/b", "a\\b", ".hidden", "a..b", "x" * 200])
def test_bad_ids_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        artifact_dir(tmp_path, bad)
    with pytest.raises(ValueError):
        artifact_dir(tmp_path, "run1", bad)


def test_redacts_sensitive_keys_nested():
    data = {
        "headers": {"Authorization": "Bearer abc", "Content-Type": "json"},
        "items": [{"api_key": "k", "ok": 1}],
    }
    out = redact(data)
    assert out["headers"]["Authorization"] == REDACTED
    assert out["headers"]["Content-Type"] == "json"
    assert out["items"][0]["api_key"] == REDACTED
    assert out["items"][0]["ok"] == 1


def test_redact_keeps_token_count_fields():
    assert redact({"max_tokens": 1000, "tokens_used": 5}) == {"max_tokens": 1000, "tokens_used": 5}


def test_redacts_bearer_in_strings():
    assert "abc123" not in redact({"log": "sent Bearer abc123 ok"})["log"]


def test_redact_does_not_mutate_input():
    data = {"password": "p"}
    redact(data)
    assert data["password"] == "p"


def test_write_json_artifact_is_redacted_and_overwritable(tmp_path):
    p = write_json_artifact(tmp_path, "run1", "c1", "result", {"cookie": "s3cr3t", "n": 1})
    assert p == tmp_path / "run1" / "c1" / "result.json"
    text = p.read_text(encoding="utf-8")
    assert "s3cr3t" not in text
    assert json.loads(text) == {"cookie": REDACTED, "n": 1}
    write_json_artifact(tmp_path, "run1", "c1", "result", {"n": 2})
    assert json.loads(p.read_text(encoding="utf-8")) == {"n": 2}
    assert not list(p.parent.glob("*.tmp"))


def test_write_rejects_traversal(tmp_path):
    with pytest.raises(ValueError):
        write_json_artifact(tmp_path, "../evil", "c1", "r", {})