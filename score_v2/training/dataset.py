"""Documented DAEMON Score v2 training dataset schema.

Two independent record kinds live in the same JSONL file but are never
merged into one object:

  * `FeatureRecord` -- one participant's `score_features.py` block for one
    game and evidence tier, plus its corpus split assignment
    (`corpus.splits.assign_splits`). This is the ONLY thing
    `score_v2.feature_spec.extract_feature_vector` may ever see, and its
    `features` field is leakage-validated on both construction and load.

    A record's identity is split in two, because the SAME game/participant
    normally has evidence in more than one tier at once (e.g. `aggregate`
    is always present, and `lcu_timeline` alongside it once captured):

      - `item_ref` (`"{game_id}:{participant_id}:{evidence_source}"`) is
        this record's own globally-unique storage key -- distinct per
        tier, so a game with both `aggregate` and `lcu_timeline` evidence
        can hold both `FeatureRecord`s without a collision.
      - `base_ref` (`"{game_id}:{participant_id}"`) is the tier-agnostic
        review reference -- exactly the ref shape
        `corpus.review.export_for_training` already produces for
        `PairLabel.left_ref`/`right_ref`. A single human pairwise
        preference is expressed once, in terms of `base_ref`, and applies
        independently to whichever tier is currently being trained/
        evaluated (see `score_v2.training.export.dataset_for_tier` and
        `TrainingDataset.feature_records_by_base_ref`) -- never silently
        collapsed onto one arbitrary tier.

  * `PairLabel` -- a de-blinded human pairwise preference between two
    `base_ref`s, exactly the row shape `corpus.review.export_for_training`
    already produces. Strictly validated on construction: `choice` must be
    one of the four values `corpus.review` supports, `relation`/
    `winner_ref` must agree with `choice`, `confidence` must be in
    `[0, 1]`, and `left_ref`/`right_ref` must be distinct.

`StateValueLabel` is a third, explicitly AUXILIARY and OFFLINE-ONLY record
carrying a team's real win/loss outcome (see its docstring below). It has
its own loader/writer path and is never merged into a `FeatureRecord` or
read by `score_v2.feature_spec` -- see
`tests/test_score_v2_dataset.py::test_state_value_labels_are_isolated_from_feature_records`.

CLI: `py -m score_v2.training.dataset validate <dataset.jsonl>`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Optional

from score_v2.leakage import assert_no_outcome_leakage

DATASET_SCHEMA_VERSION = 1

VALID_SPLITS = ("train", "validation", "test")

# Mirrors corpus.review.VALID_CHOICES -- kept as an independent constant
# (rather than importing corpus.review here) so score_v2.training stays
# importable without depending on corpus's blinded-review presentation
# machinery, while still validating exactly the same four values.
VALID_CHOICES = ("left", "right", "tie", "insufficient_evidence")


class DatasetValidationError(Exception):
    """Raised when a dataset row fails schema or leakage validation."""


def make_base_ref(game_id: int, participant_id: int) -> str:
    """Tier-agnostic review reference, matching `corpus.review`'s ref shape."""
    return f"{game_id}:{participant_id}"


def parse_base_ref(base_ref: str) -> tuple[int, int]:
    game_id_str, participant_id_str = base_ref.split(":")
    return int(game_id_str), int(participant_id_str)


def make_item_ref(game_id: int, participant_id: int, evidence_source: str) -> str:
    """Tier-specific `FeatureRecord` storage key -- unique per (game,
    participant, tier), so multiple tiers' records for the same
    game/participant can coexist in one `TrainingDataset` without a
    collision.
    """
    return f"{make_base_ref(game_id, participant_id)}:{evidence_source}"


@dataclass(frozen=True)
class FeatureRecord:
    item_ref: str
    base_ref: str
    game_id: int
    participant_id: int
    evidence_source: str
    role: str
    duration_seconds: float
    abstain: bool
    abstain_reason: Optional[str]
    chosen_source_completeness: Optional[float]
    features: Mapping  # one participant's score_features.py block
    split: Optional[str] = None

    def __post_init__(self) -> None:
        # Validate on every construction path (not just `from_dict`), so a
        # `FeatureRecord` can never exist in an invalid or leaking state --
        # this is the record type `score_v2.feature_spec` trusts most.
        self.validate()

    def to_dict(self) -> dict:
        return {
            "item_ref": self.item_ref,
            "base_ref": self.base_ref,
            "game_id": self.game_id,
            "participant_id": self.participant_id,
            "evidence_source": self.evidence_source,
            "role": self.role,
            "duration_seconds": self.duration_seconds,
            "abstain": self.abstain,
            "abstain_reason": self.abstain_reason,
            "chosen_source_completeness": self.chosen_source_completeness,
            "features": dict(self.features),
            "split": self.split,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "FeatureRecord":
        game_id = int(data["game_id"])
        participant_id = int(data["participant_id"])
        record = cls(
            item_ref=data["item_ref"],
            base_ref=data.get("base_ref", make_base_ref(game_id, participant_id)),
            game_id=game_id,
            participant_id=participant_id,
            evidence_source=data["evidence_source"],
            role=data.get("role") or "unknown",
            duration_seconds=float(data.get("duration_seconds") or 0.0),
            abstain=bool(data.get("abstain", False)),
            abstain_reason=data.get("abstain_reason"),
            chosen_source_completeness=data.get("chosen_source_completeness"),
            features=dict(data["features"]),
            split=data.get("split"),
        )
        record.validate()
        return record

    def validate(self) -> None:
        if self.split is not None and self.split not in VALID_SPLITS:
            raise DatasetValidationError(f"Unknown split {self.split!r}")
        expected_base_ref = make_base_ref(self.game_id, self.participant_id)
        if self.base_ref != expected_base_ref:
            raise DatasetValidationError(
                f"base_ref {self.base_ref!r} does not match derived "
                f"game_id:participant_id {expected_base_ref!r}"
            )
        expected_item_ref = make_item_ref(
            self.game_id, self.participant_id, self.evidence_source,
        )
        if self.item_ref != expected_item_ref:
            raise DatasetValidationError(
                f"item_ref {self.item_ref!r} does not match derived "
                f"game_id:participant_id:evidence_source {expected_item_ref!r}"
            )
        assert_no_outcome_leakage(
            self.features, context=f"FeatureRecord {self.item_ref} features",
        )


@dataclass(frozen=True)
class PairLabel:
    pair_id: str
    left_ref: str
    right_ref: str
    winner_ref: Optional[str]
    relation: str
    choice: str
    confidence: float
    rationale_tags: tuple[str, ...]
    reviewer_id: str
    created_at: str

    def __post_init__(self) -> None:
        self.validate()

    def to_dict(self) -> dict:
        return {
            "pair_id": self.pair_id,
            "left_ref": self.left_ref,
            "right_ref": self.right_ref,
            "winner_ref": self.winner_ref,
            "relation": self.relation,
            "choice": self.choice,
            "confidence": self.confidence,
            "rationale_tags": list(self.rationale_tags),
            "reviewer_id": self.reviewer_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "PairLabel":
        return cls(
            pair_id=data["pair_id"],
            left_ref=data["left_ref"],
            right_ref=data["right_ref"],
            winner_ref=data.get("winner_ref"),
            relation=data["relation"],
            choice=data["choice"],
            confidence=float(data["confidence"]),
            rationale_tags=tuple(data.get("rationale_tags", ())),
            reviewer_id=data["reviewer_id"],
            created_at=data["created_at"],
        )

    def validate(self) -> None:
        if self.choice not in VALID_CHOICES:
            raise DatasetValidationError(
                f"pair {self.pair_id!r}: unknown choice {self.choice!r}, must "
                f"be one of {VALID_CHOICES}"
            )
        if not self.left_ref or not self.right_ref:
            raise DatasetValidationError(
                f"pair {self.pair_id!r}: left_ref/right_ref must not be empty"
            )
        if self.left_ref == self.right_ref:
            raise DatasetValidationError(
                f"pair {self.pair_id!r}: left_ref and right_ref must be distinct "
                f"(both are {self.left_ref!r})"
            )
        if not (0.0 <= self.confidence <= 1.0):
            raise DatasetValidationError(
                f"pair {self.pair_id!r}: confidence must be in [0, 1], got "
                f"{self.confidence}"
            )
        if not self.reviewer_id:
            raise DatasetValidationError(f"pair {self.pair_id!r}: reviewer_id must not be empty")

        # relation/winner_ref must agree with choice -- these three fields
        # are redundant by construction (corpus.review.export_for_training
        # derives relation/winner_ref FROM choice), so a mismatch here
        # means the row was hand-edited or corrupted, not a legitimate
        # correction.
        if self.choice == "left":
            if self.relation != "left_preferred":
                raise DatasetValidationError(
                    f"pair {self.pair_id!r}: choice='left' requires "
                    f"relation='left_preferred', got {self.relation!r}"
                )
            if self.winner_ref != self.left_ref:
                raise DatasetValidationError(
                    f"pair {self.pair_id!r}: choice='left' requires "
                    f"winner_ref == left_ref"
                )
        elif self.choice == "right":
            if self.relation != "right_preferred":
                raise DatasetValidationError(
                    f"pair {self.pair_id!r}: choice='right' requires "
                    f"relation='right_preferred', got {self.relation!r}"
                )
            if self.winner_ref != self.right_ref:
                raise DatasetValidationError(
                    f"pair {self.pair_id!r}: choice='right' requires "
                    f"winner_ref == right_ref"
                )
        else:  # "tie" or "insufficient_evidence"
            if self.relation != self.choice:
                raise DatasetValidationError(
                    f"pair {self.pair_id!r}: choice={self.choice!r} requires "
                    f"relation == choice, got relation={self.relation!r}"
                )
            if self.winner_ref is not None:
                raise DatasetValidationError(
                    f"pair {self.pair_id!r}: choice={self.choice!r} requires "
                    f"winner_ref to be null"
                )


@dataclass(frozen=True)
class TrainingDataset:
    schema_version: int
    feature_records: tuple[FeatureRecord, ...]
    pair_labels: tuple[PairLabel, ...]

    def deterministic_order(self) -> "TrainingDataset":
        return TrainingDataset(
            schema_version=self.schema_version,
            feature_records=tuple(
                sorted(self.feature_records, key=lambda r: r.item_ref)
            ),
            pair_labels=tuple(
                sorted(self.pair_labels, key=lambda p: (p.pair_id, p.reviewer_id))
            ),
        )

    def feature_records_by_ref(self) -> dict[str, FeatureRecord]:
        """Item-ref-keyed view -- globally unique across every tier."""
        return {record.item_ref: record for record in self.feature_records}

    def feature_records_by_base_ref(self) -> dict[str, FeatureRecord]:
        """Base-ref-keyed view, valid only for a SINGLE-evidence-tier dataset.

        Raises `DatasetValidationError` if this dataset mixes multiple
        evidence tiers -- multi-tier lookups must go through
        `score_v2.training.export.dataset_for_tier` first, one tier at a
        time, so a review label is applied deliberately to each tier
        rather than silently collapsed onto whichever tier's record
        happens to win a `base_ref` collision in a plain dict comprehension.
        """
        tiers = {record.evidence_source for record in self.feature_records}
        if len(tiers) > 1:
            raise DatasetValidationError(
                f"feature_records_by_base_ref() requires a single-tier "
                f"dataset; found tiers {sorted(tiers)} -- call "
                "score_v2.training.export.dataset_for_tier(...) first"
            )
        return {record.base_ref: record for record in self.feature_records}

    def validate(self) -> None:
        if self.schema_version != DATASET_SCHEMA_VERSION:
            raise DatasetValidationError(
                f"Unsupported dataset schema_version {self.schema_version}"
            )
        seen_item_refs: set[str] = set()
        seen_base_refs: set[str] = set()
        for record in self.feature_records:
            record.validate()
            if record.item_ref in seen_item_refs:
                raise DatasetValidationError(
                    f"Duplicate item_ref {record.item_ref!r}"
                )
            seen_item_refs.add(record.item_ref)
            seen_base_refs.add(record.base_ref)

        seen_pair_reviewer_keys: set[tuple[str, str]] = set()
        for label in self.pair_labels:
            for ref in (label.left_ref, label.right_ref):
                if ref not in seen_base_refs:
                    raise DatasetValidationError(
                        f"pair {label.pair_id!r} references unknown "
                        f"base_ref {ref!r}"
                    )
            # Deterministic duplicate detection: the same reviewer rating
            # the same pair twice is ambiguous supervision (which row
            # wins?) and must be rejected rather than silently
            # double-counted during training. Distinct reviewers rating
            # the same pair_id is normal and expected.
            key = (label.pair_id, label.reviewer_id)
            if key in seen_pair_reviewer_keys:
                raise DatasetValidationError(
                    f"Duplicate pair label for pair_id={label.pair_id!r} "
                    f"reviewer_id={label.reviewer_id!r}"
                )
            seen_pair_reviewer_keys.add(key)

    def save_jsonl(self, path) -> None:
        ordered = self.deterministic_order()
        ordered.validate()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "kind": "score_v2_dataset_header",
                "schema_version": ordered.schema_version,
                "feature_record_count": len(ordered.feature_records),
                "pair_label_count": len(ordered.pair_labels),
            }, sort_keys=True) + "\n")
            for record in ordered.feature_records:
                row = {"kind": "feature_record"}
                row.update(record.to_dict())
                handle.write(json.dumps(row, sort_keys=True) + "\n")
            for label in ordered.pair_labels:
                row = {"kind": "pair_label"}
                row.update(label.to_dict())
                handle.write(json.dumps(row, sort_keys=True) + "\n")

    @classmethod
    def load_jsonl(cls, path) -> "TrainingDataset":
        feature_records = []
        pair_labels = []
        schema_version = DATASET_SCHEMA_VERSION
        declared_feature_record_count: Optional[int] = None
        declared_pair_label_count: Optional[int] = None
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                row = json.loads(stripped)
                kind = row.get("kind")
                payload = {key: value for key, value in row.items() if key != "kind"}
                if kind == "score_v2_dataset_header":
                    schema_version = int(
                        payload.get("schema_version", DATASET_SCHEMA_VERSION)
                    )
                    declared_feature_record_count = payload.get("feature_record_count")
                    declared_pair_label_count = payload.get("pair_label_count")
                elif kind == "feature_record":
                    feature_records.append(FeatureRecord.from_dict(payload))
                elif kind == "pair_label":
                    pair_labels.append(PairLabel.from_dict(payload))
                else:
                    raise DatasetValidationError(
                        f"line {line_number}: unknown row kind {kind!r}"
                    )
        if (
                declared_feature_record_count is not None
                and declared_feature_record_count != len(feature_records)
        ):
            raise DatasetValidationError(
                f"header declares feature_record_count="
                f"{declared_feature_record_count} but the file actually "
                f"contains {len(feature_records)} -- file may be truncated "
                "or corrupted"
            )
        if (
                declared_pair_label_count is not None
                and declared_pair_label_count != len(pair_labels)
        ):
            raise DatasetValidationError(
                f"header declares pair_label_count={declared_pair_label_count} "
                f"but the file actually contains {len(pair_labels)} -- file "
                "may be truncated or corrupted"
            )
        dataset = cls(
            schema_version=schema_version,
            feature_records=tuple(feature_records),
            pair_labels=tuple(pair_labels),
        ).deterministic_order()
        dataset.validate()
        return dataset


def select_split(dataset: TrainingDataset, split_name: str) -> TrainingDataset:
    """Restrict `dataset` to records whose `split == split_name`.

    Returns an EMPTY `TrainingDataset` (zero feature records, zero pair
    labels) if no record carries that split -- this function never falls
    back to using every record; callers that need "use everything"
    behavior must do so explicitly rather than treating an empty split as
    a signal to ignore the request. Pair labels are resolved via
    `base_ref` (tier-agnostic, matching each record's own identity) so a
    pair only survives if BOTH referenced participants have a record in
    this split.
    """
    matching_records = tuple(
        record for record in dataset.feature_records if record.split == split_name
    )
    matching_base_refs = {record.base_ref for record in matching_records}
    matching_pairs = tuple(
        pair for pair in dataset.pair_labels
        if pair.left_ref in matching_base_refs and pair.right_ref in matching_base_refs
    )
    return TrainingDataset(
        schema_version=dataset.schema_version, feature_records=matching_records,
        pair_labels=matching_pairs,
    )


def build_feature_record(
        *, game_id: int, participant_id: int, evidence_source: str,
        features_for_game: Mapping, split: Optional[str] = None) -> FeatureRecord:
    """Build one `FeatureRecord` from a whole game's `compute_feature_set` output.

    `features_for_game` is the top-level dict returned by
    `score_features.compute_feature_set` (equivalently, the `features`
    field read back from `HistoryStore.get_feature_set`) -- this slices
    out one participant's block plus the game-level fields a
    `FeatureRecord` also carries (duration, abstain, tier completeness).
    """
    from score_v2.feature_spec import resolve_role  # local import: training-only path

    participant_block = (features_for_game.get("participants") or {}).get(
        str(participant_id)
    )
    if participant_block is None:
        raise DatasetValidationError(
            f"No participant block for participant_id={participant_id} in "
            f"game {game_id}"
        )
    record = FeatureRecord(
        item_ref=make_item_ref(game_id, participant_id, evidence_source),
        base_ref=make_base_ref(game_id, participant_id),
        game_id=game_id,
        participant_id=participant_id,
        evidence_source=evidence_source,
        role=resolve_role(participant_block),
        duration_seconds=float(features_for_game.get("duration_seconds") or 0.0),
        abstain=bool(features_for_game.get("abstain", False)),
        abstain_reason=features_for_game.get("abstain_reason"),
        chosen_source_completeness=features_for_game.get("chosen_source_completeness"),
        features=participant_block,
        split=split,
    )
    record.validate()
    return record


@dataclass(frozen=True)
class StateValueLabel:
    """AUXILIARY, OFFLINE-ONLY team outcome label -- never a model feature.

    Carries exactly the game outcome (`state_value` is 1.0 for the team
    that won, 0.0 for the team that lost) that `score_v2.feature_spec`
    must never see. It is kept in its own dataclass and its own JSONL
    stream (`save_state_value_labels_jsonl`/`load_state_value_labels_jsonl`),
    never merged into a `FeatureRecord`, so that any future *external*
    validity check (e.g. "do average team scores correlate with who
    actually won?") stays structurally incapable of leaking into
    `score_v2.training.baseline`'s per-participant feature vector. No
    state-value *model* is trained in this stage -- that is out of scope
    for the per-participant DAEMON Score v2 baseline this package ships;
    see `docs/SCORE_V2_MODELS.md` "Known limitations".
    """

    game_id: int
    team_id: int
    state_value: float
    source: str
    created_at: str

    def __post_init__(self) -> None:
        if self.state_value not in (0.0, 1.0):
            raise DatasetValidationError(
                f"state_value must be 0.0 or 1.0, got {self.state_value!r}"
            )

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "team_id": self.team_id,
            "state_value": self.state_value,
            "source": self.source,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "StateValueLabel":
        return cls(
            game_id=int(data["game_id"]),
            team_id=int(data["team_id"]),
            state_value=float(data["state_value"]),
            source=data["source"],
            created_at=data["created_at"],
        )


def build_state_value_labels_from_match(match: Mapping, participants: Mapping) -> tuple:
    """Derive both teams' `StateValueLabel`s from one `HistoryStore` report.

    `match` is a `HistoryStore.get_match`/`get_report()["match"]` row
    (carries `local_win` and `local_participant_id`); `participants` is
    `get_report()["participants"]`. This is the ONLY function in this
    package allowed to read `local_win` -- callers must keep its output
    entirely separate from any `FeatureRecord`/dataset used for training.
    """
    local_participant_id = match["local_participant_id"]
    local_team_id = next(
        (p["team_id"] for p in participants if p["participant_id"] == local_participant_id),
        None,
    )
    if local_team_id is None:
        raise DatasetValidationError(
            f"No participant matches local_participant_id="
            f"{local_participant_id!r} for game {match['game_id']}"
        )
    local_win = bool(match.get("local_win"))
    team_ids = sorted({p["team_id"] for p in participants})
    created_at = str(match.get("game_creation_date") or "")
    labels = []
    for team_id in team_ids:
        won = local_win if team_id == local_team_id else not local_win
        labels.append(StateValueLabel(
            game_id=match["game_id"], team_id=team_id,
            state_value=1.0 if won else 0.0,
            source="match_aggregate_outcome", created_at=created_at,
        ))
    return tuple(labels)


def save_state_value_labels_jsonl(labels, path) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for label in sorted(labels, key=lambda label: (label.game_id, label.team_id)):
            handle.write(json.dumps(label.to_dict(), sort_keys=True) + "\n")


def load_state_value_labels_jsonl(path) -> tuple:
    labels = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                labels.append(StateValueLabel.from_dict(json.loads(stripped)))
    return tuple(sorted(labels, key=lambda label: (label.game_id, label.team_id)))


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Score v2 training dataset utilities")
    sub = parser.add_subparsers(dest="command", required=True)
    validate_parser = sub.add_parser("validate", help="Load and validate a dataset JSONL")
    validate_parser.add_argument("dataset_path")

    args = parser.parse_args()
    if args.command == "validate":
        try:
            dataset = TrainingDataset.load_jsonl(args.dataset_path)
        except (DatasetValidationError, OSError, json.JSONDecodeError) as exc:
            print(f"INVALID: {exc}")
            return 1
        print(
            f"OK: {len(dataset.feature_records)} feature records, "
            f"{len(dataset.pair_labels)} pair labels"
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
