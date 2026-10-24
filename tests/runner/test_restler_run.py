import json
from pathlib import Path

import pytest

from adaptive_restler.runner.restler_run import (
    RestlerRunError, aggregate, main, parse_bug_buckets, summarize_run,
)

FIX = Path(__file__).parent / "fixtures" / "restler_real"
KNOWN_HASHES = {
    "InvalidDynamicObjectChecker_20x_080f3c85aec4b427307e03c004ffe30a9e899238",
    "InvalidDynamicObjectChecker_20x_82a38edf44e1613514ac1ad0303a4efabdbbb47d",
    "InvalidValueChecker_500_5f9bb084cbb3a2529b26bf690142685a65bd355b",
    "PayloadBodyChecker_500_e85ef29a919c4d5b6c42c62c0c5d147f932717a1",
    "UseAfterFreeChecker_500_080f3c85aec4b427307e03c004ffe30a9e899238",
}


def _fuzzlean(**kw):
    base = dict(mode="fuzz-lean", run_id="fx-fuzzlean", target_host="127.0.0.1", target_port=8899,
                wall_seconds=3.0, exit_code=0)
    base.update(kw)
    return summarize_run(FIX / "fuzzlean", **base)


def test_fuzzlean_real_buckets():
    rec = _fuzzlean()
    assert rec["harness_errors"] == []
    assert set(rec["unique_candidate_bug_hashes"]) == KNOWN_HASHES
    assert rec["unique_candidate_bugs"] == 5
    assert rec["raw_bucket_counts"] == {
        "InvalidDynamicObjectChecker_20x": 2, "InvalidValueChecker_500": 1,
        "PayloadBodyChecker_500": 1, "UseAfterFreeChecker_500": 1,
    }
    assert rec["run_summary"]["bugCount"] == 19
    assert rec["bug_status"] == "candidate_unvalidated"
    assert rec["unique_candidate_bugs_per_minute"] == 100.0
    assert rec["requests_attempted"] is None  # fixture has no network logs


def test_testmode_real_network_log():
    rec = summarize_run(FIX / "testmode", mode="test", run_id="fx-test", target_host="127.0.0.1",
                        target_port=8899, wall_seconds=2.0, exit_code=0)
    f = rec["network"]["fuzz_log"]
    assert f["sends"] >= 6 and f["receives"] <= f["sends"]
    assert sum(f["status_counts"].values()) == f["receives"]
    assert "400" in f["status_counts"] and any(k.startswith("2") for k in f["status_counts"])
    assert rec["unique_candidate_bugs"] == 0 and rec["bug_buckets"] == []
    assert rec["restler_reported"]["coverage"] == "6 / 6"
    assert rec["requests_attempted"] >= f["sends"]


def test_missing_mode_dir_is_harness_error(tmp_path):
    rec = summarize_run(tmp_path, mode="fuzz-lean", run_id="x", target_host="h", target_port=1, exit_code=0)
    assert "mode_dir_missing" in rec["harness_errors"]
    assert rec["unique_candidate_bugs"] == 0 and rec["unique_candidate_bugs_per_minute"] is None


def test_nonzero_exit_flagged_and_no_rate():
    rec = _fuzzlean(exit_code=1)
    assert "restler_exit_nonzero" in rec["harness_errors"]
    assert rec["unique_candidate_bugs_per_minute"] is None


def test_unsupported_mode_rejected(tmp_path):
    with pytest.raises(RestlerRunError):
        summarize_run(tmp_path, mode="fuzz", run_id="x", target_host="h", target_port=1)


def test_parse_bug_buckets_ignores_summary_lines():
    text = (
        "InvalidValueChecker_500: 1\nTotal Buckets: 1\n-------------\n"
        "InvalidValueChecker_500 - Bug was reproduced - InvalidValueChecker_500_1.replay.txt\n"
        "Hash: InvalidValueChecker_500_abc\nGET /x HTTP/1.1\\r\\n\n"
        "UseAfterFreeChecker_500 - Bug was not reproduced - UseAfterFreeChecker_500_1.replay.txt\n"
        "Hash: UseAfterFreeChecker_500_def\n"
    )
    b = parse_bug_buckets(text)
    assert [(x["bucket"], x["reproduced"], x["hash"]) for x in b] == [
        ("InvalidValueChecker_500", True, "InvalidValueChecker_500_abc"),
        ("UseAfterFreeChecker_500", False, "UseAfterFreeChecker_500_def"),
    ]


def test_aggregate_detects_identical_and_different_sets():
    a, b = _fuzzlean(run_id="a"), _fuzzlean(run_id="b")
    assert aggregate([a, b])["hash_sets_identical_across_runs"] is True
    c = dict(b, unique_candidate_bug_hashes=a["unique_candidate_bug_hashes"][:2])
    agg = aggregate([a, c])
    assert agg["hash_sets_identical_across_runs"] is False
    assert agg["statistically_meaningful"] is False


def test_cli_summarize_writes_artifact(tmp_path, capsys):
    rc = main(["summarize", "--run-dir", str(FIX / "fuzzlean"), "--mode", "fuzz-lean", "--run-id", "fx-cli",
               "--target-host", "127.0.0.1", "--target-port", "8899", "--wall-seconds", "3",
               "--exit-code", "0", "--artifact-root", str(tmp_path)])
    out = capsys.readouterr().out.strip().splitlines()[-1]
    data = json.loads(Path(out).read_text(encoding="utf-8"))
    assert rc == 0 and data["unique_candidate_bugs"] == 5
