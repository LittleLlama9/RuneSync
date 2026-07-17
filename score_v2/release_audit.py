"""Fail-closed audit of a Score v2 candidate's release evidence bundle."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping

from score_v2.artifact import Artifact


AUDIT_SCHEMA_VERSION = 1
VALID_GATE_STATUSES = ("passed", "blocked", "failed")
REQUIRED_RELEASE_GATES = (
    "match_v5_verification",
    "human_pairwise_labels",
    "actual_training_leakage_scan",
    "adversarial_cases",
    "calibration_and_bootstrap",
    "coaching_human_acceptance",
    "fairness_drift_and_external_benchmark",
    "collector_and_packaged_runtime",
    "independent_artifact_review",
    "release_scope_and_todos",
)


class ReleaseAuditError(ValueError):
    """Raised when release evidence is malformed or unverifiable."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_identity(artifact: Artifact) -> dict:
    return {
        "evidence_source": artifact.evidence_source,
        "model_family": artifact.model_family,
        "model_version": artifact.model_version,
        "feature_version": artifact.feature_version,
        "calibration_version": artifact.calibration_version,
        "content_hash": artifact.content_hash,
    }


def _validate_candidate(manifest: Mapping, artifact: Artifact) -> dict:
    candidate = manifest.get("candidate")
    if not isinstance(candidate, Mapping):
        raise ReleaseAuditError("Release evidence has no candidate identity.")
    expected = _candidate_identity(artifact)
    mismatches = {
        key: {"expected": value, "actual": candidate.get(key)}
        for key, value in expected.items()
        if candidate.get(key) != value
    }
    if mismatches:
        raise ReleaseAuditError(
            f"Release evidence candidate does not match artifact: {mismatches}"
        )
    return expected


def _resolve_evidence_path(root: Path, value: str) -> Path:
    if not value:
        raise ReleaseAuditError("Passed gate has no evidence_path.")
    path = (root / value).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ReleaseAuditError(
            f"Evidence path escapes the evidence root: {value}"
        ) from exc
    return path


def audit_release_evidence(
        artifact: Artifact,
        manifest: Mapping,
        *,
        evidence_root: Path,
) -> dict:
    """Validate one immutable candidate against all required release gates."""
    if not isinstance(manifest, Mapping):
        raise ReleaseAuditError("Release evidence manifest must be an object.")
    if int(manifest.get("schema_version") or 0) != AUDIT_SCHEMA_VERSION:
        raise ReleaseAuditError(
            f"Unsupported release evidence schema "
            f"{manifest.get('schema_version')!r}."
        )
    candidate = _validate_candidate(manifest, artifact)
    raw_gates = manifest.get("gates")
    if not isinstance(raw_gates, list):
        raise ReleaseAuditError("Release evidence gates must be a list.")
    by_id = {}
    for row in raw_gates:
        if not isinstance(row, Mapping):
            raise ReleaseAuditError("Every release gate must be an object.")
        gate_id = row.get("gate_id")
        if not isinstance(gate_id, str) or not gate_id:
            raise ReleaseAuditError("Every release gate needs a string gate_id.")
        if gate_id in by_id:
            raise ReleaseAuditError(f"Duplicate release gate {gate_id!r}.")
        by_id[gate_id] = row
    missing = [
        gate_id for gate_id in REQUIRED_RELEASE_GATES
        if gate_id not in by_id
    ]
    unknown = sorted(
        gate_id for gate_id in by_id
        if gate_id not in REQUIRED_RELEASE_GATES
    )
    if missing or unknown:
        raise ReleaseAuditError(
            f"Release gate set mismatch; missing={missing}, unknown={unknown}."
        )

    root = evidence_root.resolve()
    gates = []
    for gate_id in REQUIRED_RELEASE_GATES:
        row = by_id[gate_id]
        status = row.get("status")
        summary = str(row.get("summary") or "").strip()
        if status not in VALID_GATE_STATUSES:
            raise ReleaseAuditError(
                f"Gate {gate_id} has invalid status {status!r}."
            )
        if not summary:
            raise ReleaseAuditError(f"Gate {gate_id} has no summary.")
        audited = {
            "gate_id": gate_id,
            "status": status,
            "summary": summary,
            "evidence_path": None,
            "evidence_sha256": None,
        }
        if status == "passed":
            path = _resolve_evidence_path(root, row.get("evidence_path"))
            if not path.is_file() or path.stat().st_size <= 0:
                raise ReleaseAuditError(
                    f"Gate {gate_id} evidence file is missing or empty: {path}"
                )
            actual_hash = _sha256(path)
            expected_hash = str(row.get("evidence_sha256") or "").lower()
            if actual_hash != expected_hash:
                raise ReleaseAuditError(
                    f"Gate {gate_id} evidence hash mismatch."
                )
            audited["evidence_path"] = str(path)
            audited["evidence_sha256"] = actual_hash
        gates.append(audited)

    blockers = [
        {
            "gate_id": row["gate_id"],
            "status": row["status"],
            "summary": row["summary"],
        }
        for row in gates if row["status"] != "passed"
    ]
    all_gates_passed = not blockers
    unsafe_production_flag = artifact.production_ready and not all_gates_passed
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "candidate": candidate,
        "artifact_production_ready": artifact.production_ready,
        "all_gates_passed": all_gates_passed,
        "unsafe_production_flag": unsafe_production_flag,
        "ready_for_human_promotion": (
            all_gates_passed and not artifact.production_ready
        ),
        "release_ready": all_gates_passed and artifact.production_ready,
        "blockers": blockers,
        "gates": gates,
    }
