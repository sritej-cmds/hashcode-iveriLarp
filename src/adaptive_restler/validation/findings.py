"""Finding validation (contract 5.5).

Conservative by design: this module only produces `lead` findings. Confirmation
needs reproducible evidence plus human review and is not automated here.
HTTP status alone (5xx, 401, 403, 200) never confirms anything.
"""

from __future__ import annotations

import hashlib
import re

from adaptive_restler.models import (
    ExecutionResult,
    ExecutionStatus,
    Finding,
    FindingStatus,
    ValidationContext,
)

CATEGORY_SERVER_ERROR = "server_error"
CATEGORY_TIMEOUT = "timeout"

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_LONG_HEX_RE = re.compile(r"^[0-9a-fA-F]{16,}$")


def normalize_path_template(path: str) -> str:
    """Replace ID-looking segments with {id}; drop any query string."""
    path = path.split("?", 1)[0]
    segments = []
    for seg in path.split("/"):
        if seg.isdigit() or _UUID_RE.match(seg) or _LONG_HEX_RE.match(seg):
            segments.append("{id}")
        else:
            segments.append(seg)
    return "/".join(segments)


def make_fingerprint(category: str, method: str, path: str) -> str:
    key = f"{category}|{method.upper()}|{normalize_path_template(path)}"
    return "fp-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _finding_id(run_id: str, candidate_id: str, category: str, discriminator: str) -> str:
    key = f"{run_id}|{candidate_id}|{category}|{discriminator}"
    return "f-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _status_code(response: dict) -> int | None:
    code = response.get("status_code")
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return None


def validate_findings(
    result: ExecutionResult,
    context: ValidationContext,
) -> list[Finding]:
    """Contract 5.5. Returns leads only; sorted by finding_id for determinism."""
    if result.run_id != context.run_id:
        raise ValueError("result.run_id does not match context.run_id")

    findings: dict[str, Finding] = {}
    server_errors: dict[str, list[int]] = {}  # fingerprint -> step indexes
    first_seen: dict[str, tuple[str, str]] = {}

    for response in result.responses:
        code = _status_code(response)
        if code is None or not 500 <= code <= 599:
            continue
        method = str(response.get("method", "")).upper()
        path = str(response.get("path", ""))
        if not method or not path:
            continue
        fp = make_fingerprint(CATEGORY_SERVER_ERROR, method, path)
        server_errors.setdefault(fp, []).append(int(response.get("step", -1)))
        first_seen.setdefault(fp, (method, normalize_path_template(path)))

    for fp, steps in server_errors.items():
        method, template = first_seen[fp]
        finding = Finding(
            finding_id=_finding_id(result.run_id, result.candidate_id, CATEGORY_SERVER_ERROR, fp),
            run_id=result.run_id,
            candidate_id=result.candidate_id,
            category=CATEGORY_SERVER_ERROR,
            status=FindingStatus.LEAD,
            evidence_paths=list(result.evidence_paths),
            validation_notes=(
                f"5xx response on {method} {template} at steps {sorted(steps)}. "
                "A server error alone is not proof of a vulnerability; "
                "manual investigation required."
            ),
            fingerprint=fp,
        )
        findings[finding.finding_id] = finding

    if result.status is ExecutionStatus.TIMED_OUT:
        finding = Finding(
            finding_id=_finding_id(result.run_id, result.candidate_id, CATEGORY_TIMEOUT, "run"),
            run_id=result.run_id,
            candidate_id=result.candidate_id,
            category=CATEGORY_TIMEOUT,
            status=FindingStatus.LEAD,
            evidence_paths=list(result.evidence_paths),
            validation_notes=(
                "A request timed out. A timeout alone is not proof of a "
                "vulnerability; manual investigation required."
            ),
            fingerprint=None,
        )
        findings[finding.finding_id] = finding

    return [findings[key] for key in sorted(findings)]