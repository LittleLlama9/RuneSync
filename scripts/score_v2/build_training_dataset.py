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
import datetime
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


def _assign_temporal_personal(
        entries, ratios: dict[str, float],
        evidence_hashes: dict[str, str]) -> dict[str, str]:
    """Assign whole games chronologically for a personal-model beta.

    This deliberately permits the same local player to appear across splits.
    It is therefore unsuitable for public/general model validation and exists
    only because a single user's archive is one player-connected component.
    """
    parent = {entry.entry_id: entry.entry_id for entry in entries}

    def find(entry_id):
        root = entry_id
        while parent[root] != root:
            root = parent[root]
        while parent[entry_id] != root:
            parent[entry_id], entry_id = root, parent[entry_id]
        return root

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if right_root < left_root:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root

    first_by_match = {}
    first_by_content_hash = {}
    first_by_evidence_hash = {}
    for entry in entries:
        for key, index in (
                (entry.leakage.match_group_key, first_by_match),
                (entry.content_hash, first_by_content_hash)):
            if key in index:
                union(entry.entry_id, index[key])
            else:
                index[key] = entry.entry_id
        evidence_hash = evidence_hashes.get(entry.entry_id)
        if evidence_hash:
            if evidence_hash in first_by_evidence_hash:
                union(entry.entry_id, first_by_evidence_hash[evidence_hash])
            else:
                first_by_evidence_hash[evidence_hash] = entry.entry_id

    grouped = {}
    for entry in entries:
        grouped.setdefault(find(entry.entry_id), []).append(entry)

    created_at_by_component = {}
    for component_id, component_entries in grouped.items():
        parsed_dates = []
        for entry in component_entries:
            raw_date = entry.game_metadata.game_creation_date
            if not raw_date:
                raise DatasetValidationError(
                    f"entry {entry.entry_id!r} lacks a creation date; "
                    "cannot guarantee a chronological split"
                )
            try:
                parsed = datetime.datetime.fromisoformat(
                    raw_date.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise DatasetValidationError(
                    f"entry {entry.entry_id!r} has invalid creation date "
                    f"{raw_date!r}; cannot guarantee a chronological split"
                ) from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise DatasetValidationError(
                    f"entry {entry.entry_id!r} has timezone-naive creation "
                    "date; cannot guarantee a chronological split"
                )
            parsed_dates.append(parsed.astimezone(datetime.timezone.utc))
        if not parsed_dates:
            raise DatasetValidationError(
                f"component {component_id!r} has no creation dates; "
                "cannot guarantee a chronological split"
            )
        # A duplicate observed later must not move future information into an
        # earlier split, so date the whole component by its newest member.
        created_at_by_component[component_id] = max(parsed_dates)

    ordered_components = sorted(
        grouped,
        key=lambda component_id: (
            created_at_by_component[component_id], component_id,
        ),
    )
    if len(ordered_components) < 3:
        raise DatasetValidationError(
            "temporal-personal splitting requires at least 3 independent "
            "match/content components"
        )
    ratio_total = sum(ratios.values())
    if any(ratio < 0 for ratio in ratios.values()) or ratio_total <= 0:
        raise DatasetValidationError(
            "split ratios must be non-negative and sum to more than zero"
        )
    normalized = {
        name: ratios[name] / ratio_total
        for name in ("train", "validation", "test")
    }
    n_components = len(ordered_components)
    validation_count = max(1, round(n_components * normalized["validation"]))
    test_count = max(1, round(n_components * normalized["test"]))
    train_count = n_components - validation_count - test_count
    if train_count < 1:
        raise DatasetValidationError(
            "temporal-personal split ratios leave no training games"
        )
    split_by_component = {}
    for index, component_id in enumerate(ordered_components):
        if index < train_count:
            split = "train"
        elif index < train_count + validation_count:
            split = "validation"
        else:
            split = "test"
        split_by_component[component_id] = split
    return {
        entry.entry_id: split_by_component[find(entry.entry_id)]
        for entry in entries
    }


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
        split_strategy: str = "strict-grouped",
        train_ratio: float = DEFAULT_SPLIT_RATIOS["train"],
        validation_ratio: float = DEFAULT_SPLIT_RATIOS["validation"],
        test_ratio: float = DEFAULT_SPLIT_RATIOS["test"]) -> TrainingDataset:
    manifest = CorpusManifest.load(manifest_path)
    entries = manifest.to_list()
    store = HistoryStore(history_db)
    stored_by_entry = {
        entry.entry_id: store.get_feature_set(
            entry.game_id, evidence_source=entry.source,
        )
        for entry in entries
    }
    ratios = {
        "train": train_ratio,
        "validation": validation_ratio,
        "test": test_ratio,
    }
    if split_strategy == "strict-grouped":
        config = SplitConfig(seed=split_seed, ratios=ratios)
        assignments, leakage_report = assign_splits_strict(entries, config)
        if leakage_report.warnings:
            for warning in leakage_report.warnings:
                print(f"SPLIT WARNING: {warning}", file=sys.stderr)
    elif split_strategy == "temporal-personal":
        assignments = _assign_temporal_personal(
            entries, ratios,
            {
                entry_id: stored["input_hash"]
                for entry_id, stored in stored_by_entry.items()
                if stored is not None and stored.get("input_hash")
            },
        )
        print(
            "SPLIT WARNING: temporal-personal permits the same player across "
            "chronological splits and is valid only for an opt-in personal beta",
            file=sys.stderr,
        )
    else:
        raise DatasetValidationError(
            f"unknown split_strategy {split_strategy!r}"
        )

    feature_records = []
    skipped_missing_feature_set = []
    for entry in entries:
        stored = stored_by_entry[entry.entry_id]
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
    parser.add_argument(
        "--split-strategy",
        choices=("strict-grouped", "temporal-personal"),
        default="strict-grouped",
    )
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
            token_map_path=args.token_map, split_strategy=args.split_strategy,
            train_ratio=args.train_ratio,
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
