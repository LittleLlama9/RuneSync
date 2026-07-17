"""Evaluate trained DAEMON Score v2 artifacts against a dataset split.

Loads (and hash-verifies) one `Artifact` per evidence tier from
`--artifacts-dir` (as written by `train_model.py`), scores every matching
`FeatureRecord` in `--dataset` via the real dependency-free
`score_v2.runtime.score_participant` path (so this evaluates exactly what
the shipped runtime would compute, not a training-time shortcut), and runs
the full `score_v2.training.evaluate` metric suite per tier.

By default only the `validation` split is evaluated -- training already
saw `train`, and `test` is held out for a final, separately-reviewed
check. If NO record carries the requested split, this script FAILS
(nonzero exit) rather than silently falling back to evaluating the whole
dataset -- `--split none` is the only explicit "use every record" path
(e.g. a smoke-test dataset with no split assignment).

Usage:
    py scripts/score_v2/evaluate_model.py --dataset dataset.jsonl \\
        --artifacts-dir artifacts/dev --report-out report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from score_v2.artifact import Artifact, ArtifactIntegrityError, ArtifactValidationError
from score_v2.runtime import score_participant
from score_v2.training.dataset import FeatureRecord, TrainingDataset, select_split
from score_v2.training.evaluate import EvaluationReport, evaluate_dataset
from score_v2.training.export import dataset_for_tier


def _group_by_game(records: list[FeatureRecord]) -> dict[int, dict]:
    games: dict[int, dict] = {}
    for record in records:
        game = games.setdefault(record.game_id, {
            "evidence_source": record.evidence_source,
            "abstain": record.abstain,
            "abstain_reason": record.abstain_reason,
            "chosen_source_completeness": record.chosen_source_completeness,
            "duration_seconds": record.duration_seconds,
            "participants": {},
        })
        game["participants"][str(record.participant_id)] = record.features
    return games


def evaluate_tier(
        tier_dataset: TrainingDataset, artifact: Artifact,
        *, bootstrap_seed: int, bootstrap_resamples: int) -> EvaluationReport:
    games = _group_by_game(list(tier_dataset.feature_records))
    scores: dict[str, float] = {}
    confidences: dict[str, float] = {}
    for record in tier_dataset.feature_records:
        result = score_participant(artifact, games[record.game_id], record.participant_id)
        # Keyed by base_ref (tier-agnostic), matching PairLabel refs --
        # NOT item_ref, which carries a tier suffix that would never match.
        scores[record.base_ref] = result.score
        confidences[record.base_ref] = result.confidence
    return evaluate_dataset(
        tier_dataset, scores, confidences,
        bootstrap_seed=bootstrap_seed, bootstrap_resamples=bootstrap_resamples,
    )


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--artifacts-dir", required=True, type=Path)
    parser.add_argument("--split", default="validation", help="'none' to use every record")
    parser.add_argument("--bootstrap-seed", type=int, default=1337)
    parser.add_argument("--bootstrap-resamples", type=int, default=200)
    parser.add_argument("--report-out", type=Path, default=None)
    args = parser.parse_args()

    dataset = TrainingDataset.load_jsonl(args.dataset)
    if args.split.lower() == "none":
        eval_dataset = dataset
    else:
        eval_dataset = select_split(dataset, args.split)
        if not eval_dataset.feature_records:
            print(
                f"FAILED: no records carry split={args.split!r}; refusing to "
                "silently evaluate on the full dataset. Pass --split none to "
                "use every record explicitly.", file=sys.stderr,
            )
            return 1

    report: dict[str, dict] = {}
    for artifact_path in sorted(args.artifacts_dir.glob("*.json")):
        evidence_source = artifact_path.stem
        try:
            artifact = Artifact.load(artifact_path)
        except (ArtifactIntegrityError, ArtifactValidationError, OSError, json.JSONDecodeError) as exc:
            report[evidence_source] = {"error": str(exc)}
            print(f"REJECTED {artifact_path}: {exc}", file=sys.stderr)
            continue

        tier_dataset = dataset_for_tier(eval_dataset, evidence_source)
        if not tier_dataset.feature_records:
            report[evidence_source] = {"status": "no_records_for_split"}
            continue
        evaluation = evaluate_tier(
            tier_dataset, artifact, bootstrap_seed=args.bootstrap_seed,
            bootstrap_resamples=args.bootstrap_resamples,
        )
        report[evidence_source] = {
            "production_ready": artifact.production_ready,
            "training_status": artifact.training_metadata.get("status"),
            "evaluation": evaluation.to_dict(),
        }

    print(json.dumps(report, indent=2, sort_keys=True))
    if args.report_out:
        args.report_out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
