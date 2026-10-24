import pytest

from adaptive_restler.runner.target_guard import (
    TargetNotAllowedError,
    check_target_url,
)

ALLOW = ["example.com"]


def test_exact_host_allowed():
    check_target_url("https://example.com/api/v1", ALLOW)


def test_case_and_trailing_dot_normalized():
    check_target_url("https://EXAMPLE.com./x", ALLOW)


@pytest.mark.parametrize(
    "url",
    [
        "https://other.com/",
        "https://example.com.evil.com/",
        "https://evilexample.com/",
        "https://sub.example.com/",
    ],
)
def test_other_hosts_rejected(url):
    with pytest.raises(TargetNotAllowedError):
        check_target_url(url, ALLOW)


def test_empty_allowlist_rejects_everything():
    with pytest.raises(TargetNotAllowedError):
        check_target_url("https://example.com/", [])


@pytest.mark.parametrize("url", ["ftp://example.com/", "file:///etc/passwd", "example.com"])
def test_bad_scheme_rejected(url):
    with pytest.raises(TargetNotAllowedError):
        check_target_url(url, ALLOW)


def test_userinfo_rejected():
    with pytest.raises(TargetNotAllowedError):
        check_target_url("https://user:pw@example.com/", ALLOW)


def test_userinfo_trick_resolves_to_real_host():
    with pytest.raises(TargetNotAllowedError):
        check_target_url("https://example.com@evil.com/", ALLOW)


def test_backslash_and_whitespace_rejected():
    for url in ["https://example.com\\@evil.com/", "https://example.com/ x"]:
        with pytest.raises(TargetNotAllowedError):
            check_target_url(url, ALLOW)


def test_port_pinned_entry():
    check_target_url("http://localhost:8080/x", ["localhost:8080"])
    with pytest.raises(TargetNotAllowedError):
        check_target_url("http://localhost:9999/x", ["localhost:8080"])


def test_portless_entry_allows_any_port():
    check_target_url("http://localhost:9999/x", ["localhost"])


def test_default_port_matches_pinned_entry():
    check_target_url("https://example.com/", ["example.com:443"])


def test_invalid_port_rejected():
    with pytest.raises(TargetNotAllowedError):
        check_target_url("http://example.com:99999/", ALLOW)


def test_invalid_allowlist_entry_raises_value_error():
    with pytest.raises(ValueError):
        check_target_url("https://a.example.com/", ["*.example.com"])


def test_error_does_not_leak_query_secrets():
    with pytest.raises(TargetNotAllowedError) as exc:
        check_target_url("https://evil.com/?token=abc123", ALLOW)
    assert "abc123" not in str(exc.value)