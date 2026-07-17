"""Compare DAEMON Score v2's four monotonic model families
(linear/GAM/boosted-stumps/monotonic-tree) per evidence tier, on a
dataset built by `build_training_dataset.py`.

Prints one JSON comparison report per evidence tier present in the
dataset: every candidate family's eligibility, train-only fit metadata,
validation metrics (the ONLY basis for selection), the selected family
(or `null` if none was eligible/scoreable), and -- only for the selected
family -- a reserved, previously-untouched test-split evaluation.

This script NEVER writes a production artifact. By default it does not
write any artifact file at all -- it only prints/saves the comparison
report JSON. `--export-selected-dir` is an explicit, clearly-labeled
OPT-IN that additionally saves the winning candidate's artifact (always
`production_ready=False`, with release notes stating it is
comparison-only) -- useful for round-trip inspection, never a release
step.

By default, TRAIN/VALIDATION/TEST use the `"train"`/`"validation"`/
`"test"` corpus splits assigned by `build_training_dataset.py
--split-seed`. If a tier has NO records for `--train-split`, this script
reports that tier `status="insufficient_data"` (never silently trains on
the full dataset) -- there is no `--split none` escape hatch here,
because comparison intrinsically needs three genuinely disjoint splits;
callers with an unsplit smoke-test dataset should assign explicit splits
first (see `build_training_dataset.py`).

Usage:
    py scripts/score_v2/compare_models.py --dataset dataset.jsonl \\
        --model-version 0.1.0-dev --calibration-version 0.1.0-dev
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import datetime

from score_features import FEATURE_VERSION
from score_v2.training.compare import (
    DEFAULT_TEST_SPLIT,
    DEFAULT_TRAIN_SPLIT,
    DEFAULT_VALIDATION_SPLIT,
    compare_all_tiers,
)
from score_v2.training.dataset import TrainingDataset


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--calibration-version", required=True)
    parser.add_argument("--feature-version", default=FEATURE_VERSION)
    parser.add_argument("--train-split", default=DEFAULT_TRAIN_SPLIT)
    parser.add_argument("--validation-split", default=DEFAULT_VALIDATION_SPLIT)
    parser.add_argument("--test-split", default=DEFAULT_TEST_SPLIT)
    parser.add_argument(
        "--include-abstained", action="store_true",
        help="Fit/calibrate on abstained (e.g. short-game) records too -- off by default.",
    )
    parser.add_argument("--bootstrap-seed", type=int, default=1337)
    parser.add_argument("--bootstrap-resamples", type=int, default=200)
    parser.add_argument("--report-out", type=Path, default=None)
    parser.add_argument(
        "--export-selected-dir", type=Path, default=None,
        help=(
            "Explicit opt-in: also save the winning candidate's artifact "
            "(as '<dir>/<evidence_source>.compare_selected.json', always "
            "production_ready=False) for round-trip inspection. Off by default."
        ),
    )
    args = parser.parse_args()

    dataset = TrainingDataset.load_jsonl(args.dataset)

    # Pinned once and reused for both the comparison run and (if requested)
    # the export re-derivation below -- otherwise the two artifacts'
    # `created_at` timestamps (and therefore their content_hash) would
    # differ even though every other input is identical.
    run_now = datetime.datetime.now(datetime.timezone.utc)

    results = compare_all_tiers(
        dataset, model_version=args.model_version, feature_version=args.feature_version,
        calibration_version=args.calibration_version,
        train_split=args.train_split, validation_split=args.validation_split,
        test_split=args.test_split, include_abstained=args.include_abstained,
        bootstrap_seed=args.bootstrap_seed, bootstrap_resamples=args.bootstrap_resamples,
        now=run_now,
    )

    report = {evidence_source: result.to_dict() for evidence_source, result in results.items()}

    print(json.dumps(report, indent=2, sort_keys=True))
    if args.report_out:
        args.report_out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    if not report:
        print(
            "NOTE: dataset contained zero feature records for any evidence "
            "tier -- nothing was compared.", file=sys.stderr,
        )
    elif all(result.selected_model is None for result in results.values()):
        print(
            "NOTE: every tier selected no model (insufficient_data or no "
            "eligible/scoreable candidate) -- expected for the current "
            "tiny/blocked corpus (see the vault decision gating final "
            "Score v2 validation on Match-V5 authorization). This script "
            "never fabricates a winner.", file=sys.stderr,
        )

    if args.export_selected_dir:
        # This is the ONE explicit opt-in path that writes any artifact to
        # disk from this script. `compare_tier` itself never returns
        # artifact objects (only their content_hash, to avoid tempting a
        # caller into treating a comparison report as a release artifact)
        # -- `build_artifact_for_family` re-derives the exact same,
        # deterministic artifact for the already-selected family.
        from score_v2.training.compare import build_artifact_for_family

        args.export_selected_dir.mkdir(parents=True, exist_ok=True)
        for evidence_source, result in results.items():
            if result.selected_model is None:
                continue
            artifact = build_artifact_for_family(
                dataset, evidence_source, result.selected_model,
                model_version=args.model_version, feature_version=args.feature_version,
                calibration_version=args.calibration_version,
                train_split=args.train_split, include_abstained=args.include_abstained,
                now=run_now,
            )
            assert artifact.content_hash == result.selected_artifact_content_hash, (
                "non-deterministic re-derivation while exporting -- refusing to save"
            )
            artifact_path = args.export_selected_dir / f"{evidence_source}.compare_selected.json"
            artifact.save(artifact_path)
            print(f"Exported comparison-selected artifact: {artifact_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
