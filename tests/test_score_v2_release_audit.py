import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from score_features import FEATURE_VERSION
from score_v2.artifact import FeatureCoefficient, RoleCalibration, build_artifact
from score_v2.feature_spec import feature_contract_for_tier
from score_v2.release_audit import (
    REQUIRED_RELEASE_GATES,
    ReleaseAuditError,
    audit_release_evidence,
)


ROOT = Path(__file__).parent.parent


def _artifact(production_ready=False):
    return build_artifact(
        model_version="2.0.0-candidate",
        feature_version=FEATURE_VERSION,
        calibration_version="2.0.0-cal",
        evidence_source="aggregate",
        intercept=0.0,
        coefficients=tuple(
            FeatureCoefficient(
                spec=spec,
                coefficient=0.2 * spec.direction,
                robust_center=0.0,
                robust_scale=1.0,
            )
            for spec in feature_contract_for_tier("aggregate")
        ),
        role_calibration={
            role: RoleCalibration(
                offset=0.0, sample_count=100, shrinkage_weight=0.9,
            )
            for role in ("top", "jungle", "mid", "bot", "support", "unknown")
        },
        score_calibration={
            "midpoint": 50.0, "scale": 5.0,
            "clip_min": 0.0, "clip_max": 100.0,
        },
        confidence_params={
            "missing_feature_penalty": 0.5,
            "evidence_quality_weight": 0.5,
            "interval_min_half_width": 3.0,
            "interval_max_half_width": 40.0,
        },
        abstention_params={
            "short_game_seconds": 600.0,
            "min_present_feature_fraction": 0.3,
            "min_confidence_to_report": 0.15,
        },
        training_metadata={"status": "fitted", "n_items": 1000},
        production_ready=production_ready,
        release_notes="release audit test artifact",
    )


def _candidate(artifact):
    return {
        "evidence_source": artifact.evidence_source,
        "model_family": artifact.model_family,
        "model_version": artifact.model_version,
        "feature_version": artifact.feature_version,
        "calibration_version": artifact.calibration_version,
        "content_hash": artifact.content_hash,
    }


def _manifest(artifact, status="blocked"):
    return {
        "schema_version": 1,
        "candidate": _candidate(artifact),
        "gates": [
            {
                "gate_id": gate_id,
                "status": status,
                "summary": f"{gate_id} {status}",
            }
            for gate_id in REQUIRED_RELEASE_GATES
        ],
    }


def _pass_all(manifest, root):
    evidence = root / "evidence.txt"
    evidence.write_text("independently verified evidence\n", encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    for gate in manifest["gates"]:
        gate.update({
            "status": "passed",
            "summary": f"{gate['gate_id']} independently passed",
            "evidence_path": "evidence.txt",
            "evidence_sha256": digest,
        })


def test_all_hashed_gates_make_development_artifact_ready_for_human_promotion(tmp_path):
    artifact = _artifact()
    manifest = _manifest(artifact)
    _pass_all(manifest, tmp_path)

    report = audit_release_evidence(
        artifact, manifest, evidence_root=tmp_path,
    )

    assert report["all_gates_passed"] is True
    assert report["ready_for_human_promotion"] is True
    assert report["release_ready"] is False
    assert report["unsafe_production_flag"] is False


def test_blocked_gate_keeps_candidate_fail_closed(tmp_path):
    artifact = _artifact()
    manifest = _manifest(artifact)

    report = audit_release_evidence(
        artifact, manifest, evidence_root=tmp_path,
    )

    assert report["all_gates_passed"] is False
    assert len(report["blockers"]) == len(REQUIRED_RELEASE_GATES)
    assert report["ready_for_human_promotion"] is False


def test_production_ready_artifact_with_blocker_is_flagged_unsafe(tmp_path):
    artifact = _artifact(production_ready=True)
    manifest = _manifest(artifact)

    report = audit_release_evidence(
        artifact, manifest, evidence_root=tmp_path,
    )

    assert report["unsafe_production_flag"] is True
    assert report["release_ready"] is False


def test_candidate_identity_and_evidence_hash_are_verified(tmp_path):
    artifact = _artifact()
    manifest = _manifest(artifact)
    _pass_all(manifest, tmp_path)
    manifest["candidate"]["content_hash"] = "wrong"

    with pytest.raises(ReleaseAuditError, match="does not match artifact"):
        audit_release_evidence(artifact, manifest, evidence_root=tmp_path)

    manifest["candidate"] = _candidate(artifact)
    manifest["gates"][0]["evidence_sha256"] = "0" * 64
    with pytest.raises(ReleaseAuditError, match="hash mismatch"):
        audit_release_evidence(artifact, manifest, evidence_root=tmp_path)


def test_gate_set_and_evidence_paths_are_fail_closed(tmp_path):
    artifact = _artifact()
    manifest = _manifest(artifact)
    manifest["gates"].pop()
    with pytest.raises(ReleaseAuditError, match="gate set mismatch"):
        audit_release_evidence(artifact, manifest, evidence_root=tmp_path)

    manifest = _manifest(artifact)
    _pass_all(manifest, tmp_path)
    manifest["gates"][0]["evidence_path"] = "../outside.txt"
    with pytest.raises(ReleaseAuditError, match="escapes"):
        audit_release_evidence(artifact, manifest, evidence_root=tmp_path)

    manifest = _manifest(artifact)
    manifest["gates"][0]["gate_id"] = None
    with pytest.raises(ReleaseAuditError, match="string gate_id"):
        audit_release_evidence(artifact, manifest, evidence_root=tmp_path)


def test_release_audit_cli_writes_blocked_report_and_returns_two(tmp_path):
    artifact = _artifact()
    artifact_path = tmp_path / "artifact.json"
    artifact.save(artifact_path)
    manifest_path = tmp_path / "evidence.json"
    manifest_path.write_text(
        json.dumps(_manifest(artifact)), encoding="utf-8",
    )
    output = tmp_path / "audit.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "score_v2" / "audit_release.py"),
            "--artifact", str(artifact_path),
            "--evidence", str(manifest_path),
            "--output", str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["all_gates_passed"] is False
    assert report["artifact_production_ready"] is False


def test_release_audit_cli_rejects_nonobject_manifest_without_traceback(tmp_path):
    artifact_path = tmp_path / "artifact.json"
    _artifact().save(artifact_path)
    manifest_path = tmp_path / "evidence.json"
    manifest_path.write_text("null", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "score_v2" / "audit_release.py"),
            "--artifact", str(artifact_path),
            "--evidence", str(manifest_path),
            "--output", str(tmp_path / "audit.json"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stderr.startswith("FAILED: ")
    assert "manifest must be an object" in completed.stderr
    assert "Traceback" not in completed.stderr
