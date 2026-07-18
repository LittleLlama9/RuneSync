"""Train DAEMON Score v2 development artifacts for every evidence tier
present in a dataset built by `build_training_dataset.py`.

Writes one immutable, hashed `Artifact` JSON file per tier
(`<output-dir>/<evidence_source>.json`) plus a plain-text/JSON summary.
Every artifact this script writes has `production_ready=False` --
`score_v2.training.export.train_tier` refuses to set it otherwise, and
this script does not override that. See
`docs/SCORE_V2_MODEL_CARD_TEMPLATE.md` for the actual release gates.

By default, training uses only records/pairs whose corpus split is
`"train"` (see `build_training_dataset.py --split-seed`). If NO record
carries that split, this script FAILS (nonzero exit) rather than
silently falling back to training on the entire dataset -- `--split
none` is the only explicit "use every record" path (e.g. a dataset with
no split assignment yet, such as a quick local smoke test).

Usage:
    py scripts/score_v2/train_model.py --dataset dataset.jsonl \\
        --output-dir artifacts/dev --model-version 0.1.0-dev \\
        --calibration-version 0.1.0-dev
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from score_features import FEATURE_VERSION
from score_v2.training.baseline import (
    DEFAULT_GRADIENT_TOLERANCE,
    DEFAULT_ITERATIONS,
    DEFAULT_L2_LAMBDA,
    DEFAULT_LEARNING_RATE,
    DEFAULT_LOSS_TOLERANCE,
    DEFAULT_TIE_WEIGHT,
)
from score_v2.training.dataset import TrainingDataset, select_split
from score_v2.training.export import MIN_PAIRS_FOR_NONTRIVIAL_FIT, train_all_tiers


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--calibration-version", required=True)
    parser.add_argument("--feature-version", default=FEATURE_VERSION)
    parser.add_argument("--split", default="train", help="'none' to use every record")
    parser.add_argument("--l2-lambda", type=float, default=DEFAULT_L2_LAMBDA)
    parser.add_argument("--learning-rate", type=float, default=DEFAULT_LEARNING_RATE)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--tie-weight", type=float, default=DEFAULT_TIE_WEIGHT)
    parser.add_argument("--loss-tolerance", type=float, default=DEFAULT_LOSS_TOLERANCE)
    parser.add_argument("--gradient-tolerance", type=float, default=DEFAULT_GRADIENT_TOLERANCE)
    parser.add_argument(
        "--min-pairs-for-nontrivial-fit", type=int, default=MIN_PAIRS_FOR_NONTRIVIAL_FIT,
    )
    parser.add_argument(
        "--include-abstained", action="store_true",
        help="Train/calibrate on abstained (e.g. short-game) records too -- off by default.",
    )
    parser.add_argument(
        "--score-scale-multiplier", type=float, default=1.0,
        help="Widen the fitted tanh score-mapping scale by this factor "
             "(default 1.0 = unchanged). ~2.0 pulls heavy tails off the "
             "0/100 rails while preserving ordering; see "
             "score_v2.training.calibration.fit_score_calibration_for_score_fn.",
    )
    parser.add_argument("--summary-out", type=Path, default=None)
    args = parser.parse_args()

    dataset = TrainingDataset.load_jsonl(args.dataset)
    if args.split.lower() == "none":
        training_dataset = dataset
    else:
        training_dataset = select_split(dataset, args.split)
        if not training_dataset.feature_records:
            print(
                f"FAILED: no records carry split={args.split!r}; refusing to "
                "silently train on the full dataset. Pass --split none to "
                "use every record explicitly.", file=sys.stderr,
            )
            return 1

    results = train_all_tiers(
        training_dataset, model_version=args.model_version,
        feature_version=args.feature_version, calibration_version=args.calibration_version,
        l2_lambda=args.l2_lambda, learning_rate=args.learning_rate,
        iterations=args.iterations, tie_weight=args.tie_weight,
        loss_tolerance=args.loss_tolerance, gradient_tolerance=args.gradient_tolerance,
        min_pairs_for_nontrivial_fit=args.min_pairs_for_nontrivial_fit,
        include_abstained=args.include_abstained,
        scale_sigma_multiplier=args.score_scale_multiplier,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary = {}
    for evidence_source, result in results.items():
        artifact_path = args.output_dir / f"{evidence_source}.json"
        result.artifact.save(artifact_path)
        summary[evidence_source] = {
            "status": result.status, "n_items": result.n_items,
            "n_pairs_used": result.n_pairs_used, "n_pairs_skipped": result.n_pairs_skipped,
            "notes": result.notes, "content_hash": result.artifact.content_hash,
            "production_ready": result.artifact.production_ready,
            "artifact_path": str(artifact_path),
        }

    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.summary_out:
        args.summary_out.write_text(
            json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8",
        )

    if not summary:
        print(
            "NOTE: dataset contained zero feature records for any evidence "
            "tier -- nothing was trained.", file=sys.stderr,
        )
    elif all(result.status == "insufficient_data" for result in results.values()):
        print(
            "NOTE: every trained tier is 'insufficient_data' -- this is "
            "expected for the current tiny/blocked corpus (see the vault "
            "decision gating final Score v2 validation on Match-V5 "
            "authorization). None of these artifacts are production-ready.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
