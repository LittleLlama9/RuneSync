"""Audit a Score v2 artifact against a complete hashed evidence manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from score_v2.artifact import (
    Artifact,
    ArtifactIntegrityError,
    ArtifactValidationError,
)
from score_v2.release_audit import ReleaseAuditError, audit_release_evidence


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    try:
        artifact = Artifact.load(args.artifact)
        manifest = json.loads(args.evidence.read_text(encoding="utf-8"))
        report = audit_release_evidence(
            artifact, manifest, evidence_root=args.evidence.parent,
        )
        _write_json(args.output, report)
    except (
            ArtifactIntegrityError, ArtifactValidationError, OSError,
            json.JSONDecodeError, ReleaseAuditError, TypeError, ValueError,
    ) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({
        "all_gates_passed": report["all_gates_passed"],
        "artifact_production_ready": report["artifact_production_ready"],
        "ready_for_human_promotion": report["ready_for_human_promotion"],
        "release_ready": report["release_ready"],
        "unsafe_production_flag": report["unsafe_production_flag"],
        "blocker_count": len(report["blockers"]),
    }, indent=2, sort_keys=True))
    if report["unsafe_production_flag"]:
        return 3
    if not report["all_gates_passed"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
