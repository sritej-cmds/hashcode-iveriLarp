"""Importer vs REAL RESTler 9.3.1 replay files (captured from a fuzz-lean run on the local demo server)."""
from pathlib import Path

from adaptive_restler.models import TargetConfig
from adaptive_restler.runner.restler_adapter import import_replay_logs

FIX = Path(__file__).parent / "fixtures" / "restler_real" / "fuzzlean" / "FuzzLean" / "RestlerResults"
TARGET = TargetConfig(base_url="http://localhost:8899", allowed_hosts=["localhost"], dry_run=True)


def _paths():
    return sorted(FIX.glob("*/bug_buckets/*.replay.txt"))


def test_real_replay_files_import_without_rejections():
    paths = _paths()
    assert len(paths) == 5
    res = import_replay_logs(paths, target=TARGET)
    assert [(r.source_name, r.code, r.detail) for r in res.rejections] == []
    assert len(res.candidates) == 5
    assert sorted(len(c.sequence) for c in res.candidates) == [1, 2, 2, 2, 3]


def test_real_use_after_free_sequence_shape_and_recorded_statuses():
    res = import_replay_logs(_paths(), target=TARGET)
    uaf = [c for c in res.candidates if len(c.sequence) == 3][0]
    assert [r.method for r in uaf.sequence] == ["POST", "DELETE", "GET"]
    assert [p.recorded_status for p in res.provenance[uaf.candidate_id].requests] == [201, 204, 500]
