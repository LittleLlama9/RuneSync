"""Build a DAEMON Score v2 training dataset JSONL from local evidence.

Reads (never writes) a `HistoryStore` database and a `corpus` manifest
(see `corpus/manifest.py`/`corpus/build_from_history.py`), assigns
grouped train/validation/test splits (`corpus/splits.py`), pulls each
manifest entry's already-persisted `score_features.py` feature set back
out of the store (`HistoryStore.get_feature_set`), and writes one
`score_v2.training.dataset.FeatureRecord` per participant per manifest
entry. Optionally also folds in blinded pairwise review labels
(`corpus/review.py`'s append-only label store + token map) as
`PairLabel`s.

This script never invents a feature set for a game that was never
extracted, never invents a split assignment that bypasses leakage
checking, and never invents a pairwise label -- an empty `--labels` file
(the honest current state of this corpus) simply produces a dataset with
zero `PairLabel`s, which downstream training reports as
`"insufficient_data"` rather than silently fabricating supervision.

Usage:
    py scripts/score_v2/build_training_dataset.py \\
        --history-db path/to/history.db --manifest path/to/manifest.json \\
        --split-seed dev-2026-07 --output dataset.jsonl \\
        [--labels labels.jsonl --token-map token_map.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from corpus.manifest import CorpusManifest
from corpus.review import ReviewLabelStore, export_for_training
from corpus.splits import DEFAULT_SPLIT_RATIOS, SplitConfig, SplitLeakageError, assign_splits_strict
from history_store import HistoryStore
from score_v2.training.dataset import (
    DatasetValidationError,
    PairLabel,
    TrainingDataset,
    build_feature_record,
)


def _load_pair_labels(labels_path: Path, token_map_path: Path) -> tuple[PairLabel, ...]:
    store = ReviewLabelStore(labels_path)
    token_maps = json.loads(token_map_path.read_text(encoding="utf-8"))
    rows = export_for_training(store.all_labels(), token_maps, on_missing_mapping="skip")
    pairs = []
    for row in rows:
        if row["winner_ref"] is None and row["choice"] not in ("tie", "insufficient_evidence"):
            continue
        pairs.append(PairLabel(
            pair_id=row["pair_id"], left_ref=row["left_ref"], right_ref=row["right_ref"],
            winner_ref=row["winner_ref"], relation=row["relation"], choice=row["choice"],
            confidence=row["confidence"], rationale_tags=tuple(row["rationale_tags"]),
            reviewer_id=row["reviewer_id"], created_at=row["created_at"],
        ))
    if store.last_load_errors:
        print(
            f"WARNING: {len(store.last_load_errors)} malformed review label "
            "lines were skipped", file=sys.stderr,
        )
    return tuple(pairs)


def build_dataset(
        *, history_db: Path, manifest_path: Path, split_seed: str,
        labels_path: Path = None, token_map_path: Path = None,
        train_ratio: float = DEFAULT_SPLIT_RATIOS["train"],
        validation_ratio: float = DEFAULT_SPLIT_RATIOS["validation"],
        test_ratio: float = DEFAULT_SPLIT_RATIOS["test"]) -> TrainingDataset:
    manifest = CorpusManifest.load(manifest_path)
    entries = manifest.to_list()
    config = SplitConfig(
        seed=split_seed,
        ratios={"train": train_ratio, "validation": validation_ratio, "test": test_ratio},
    )
    assignments, leakage_report = assign_splits_strict(entries, config)
    if leakage_report.warnings:
        for warning in leakage_report.warnings:
            print(f"SPLIT WARNING: {warning}", file=sys.stderr)

    store = HistoryStore(history_db)
    feature_records = []
    skipped_missing_feature_set = []
    for entry in entries:
        stored = store.get_feature_set(entry.game_id, evidence_source=entry.source)
        if stored is None:
            skipped_missing_feature_set.append(entry.entry_id)
            continue
        split = assignments[entry.entry_id]
        participants = (stored["features"].get("participants") or {})
        for participant_id_str in participants:
            record = build_feature_record(
                game_id=entry.game_id, participant_id=int(participant_id_str),
                evidence_source=entry.source, features_for_game=stored["features"],
                split=split,
            )
            feature_records.append(record)

    pair_labels: tuple[PairLabel, ...] = ()
    if labels_path is not None and token_map_path is not None:
        pair_labels = _load_pair_labels(labels_path, token_map_path)
        # PairLabel refs are tier-agnostic base refs (matching
        # corpus.review's "{game_id}:{participant_id}" shape), NOT the
        # tier-specific `item_ref` -- see FeatureRecord.base_ref. A single
        # human label is kept once here and applied per-tier later by
        # score_v2.training.export.dataset_for_tier, not collapsed here.
        known_base_refs = {record.base_ref for record in feature_records}
        pair_labels = tuple(
            pair for pair in pair_labels
            if pair.left_ref in known_base_refs and pair.right_ref in known_base_refs
        )

    if skipped_missing_feature_set:
        print(
            f"NOTE: {len(skipped_missing_feature_set)} manifest entries had "
            "no stored feature set and were skipped: "
            f"{skipped_missing_feature_set}", file=sys.stderr,
        )

    dataset = TrainingDataset(
        schema_version=1, feature_records=tuple(feature_records), pair_labels=pair_labels,
    ).deterministic_order()
    dataset.validate()
    return dataset


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-db", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--split-seed", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--labels", type=Path, default=None)
    parser.add_argument("--token-map", type=Path, default=None)
    parser.add_argument("--train-ratio", type=float, default=DEFAULT_SPLIT_RATIOS["train"])
    parser.add_argument(
        "--validation-ratio", type=float, default=DEFAULT_SPLIT_RATIOS["validation"],
    )
    parser.add_argument("--test-ratio", type=float, default=DEFAULT_SPLIT_RATIOS["test"])
    args = parser.parse_args()

    if bool(args.labels) != bool(args.token_map):
        parser.error("--labels and --token-map must be supplied together")

    try:
        dataset = build_dataset(
            history_db=args.history_db, manifest_path=args.manifest,
            split_seed=args.split_seed, labels_path=args.labels,
            token_map_path=args.token_map, train_ratio=args.train_ratio,
            validation_ratio=args.validation_ratio, test_ratio=args.test_ratio,
        )
    except (SplitLeakageError, DatasetValidationError) as exc:
        print(f"FAILED: {exc}")
        return 1

    dataset.save_jsonl(args.output)
    by_split: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for record in dataset.feature_records:
        by_split[record.split or "unassigned"] = by_split.get(record.split or "unassigned", 0) + 1
        by_source[record.evidence_source] = by_source.get(record.evidence_source, 0) + 1
    print(
        f"OK: wrote {len(dataset.feature_records)} feature records "
        f"({by_split}) and {len(dataset.pair_labels)} pair labels across "
        f"tiers {by_source} to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
