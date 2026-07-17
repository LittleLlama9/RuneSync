"""Blinded pairwise review workflow for DAEMON Score v2.

A reviewer is shown two anonymized performance views (``left``/``right``,
randomized per pair with a seeded, deterministic coin flip) and must choose
``left``, ``right``, ``tie``, or ``insufficient_evidence`` with a confidence
level and at least one rationale tag from a controlled vocabulary. The
presented views are built with a strict field allowlist
(:data:`ALLOWED_PRESENTATION_FIELDS`) -- player name, win/loss, DAEMON
score, match rank, and any companion (e.g. OP.GG/u.gg) score are never
copied into a presentation, by construction rather than by a denylist.

Labels are append-only (:class:`ReviewLabelStore` never rewrites or deletes
a line) so the label history itself is an audit trail. De-blinding (mapping
an opaque presentation token back to a real ``item_ref``) is a separate,
explicit step (:func:`export_for_training`) that requires the private
token map produced at presentation time -- the reviewer-facing path never
needs or sees it.
"""

from __future__ import annotations

import datetime
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional, Sequence

from corpus._privacy import scan_for_forbidden
from corpus.manifest import sha256_hex

# Strict allowlist: only these fields are ever copied from a raw performance
# record into a reviewer-facing presentation. Anything not in this list
# (puuid, summoner_name, win, total_score, match_rank, companion_score,
# participant_id, game_id, team_id, ...) is dropped, not merely hidden.
ALLOWED_PRESENTATION_FIELDS = (
    "champion_name",
    "role",
    "champion_level",
    "kills",
    "deaths",
    "assists",
    "gold_earned",
    "cs",
    "damage_to_champions",
    "damage_to_objectives",
    "damage_to_turrets",
    "damage_taken",
    "damage_mitigated",
    "healing",
    "vision_score",
    "wards_placed",
    "wards_killed",
    "duration_seconds",
    "patch",
)

VALID_CHOICES = ("left", "right", "tie", "insufficient_evidence")

VALID_RATIONALE_TAGS = (
    "combat_impact",
    "objective_control",
    "vision_control",
    "economy_efficiency",
    "survivability",
    "role_context",
    "teamfight_presence",
    "laning_phase",
    "utility_support",
    "insufficient_data",
)


class ReviewValidationError(Exception):
    """Raised when a label, presentation, or export request is invalid."""


def redact_for_presentation(record: Mapping) -> dict:
    """Copy only allowlisted fields out of a raw performance record."""
    view = {key: record[key] for key in ALLOWED_PRESENTATION_FIELDS if key in record}
    problems = scan_for_forbidden(view)
    if problems:
        # Should be structurally unreachable given the allowlist copy above;
        # kept as a defense-in-depth safety net.
        raise ReviewValidationError(
            "redacted view still contains disallowed data: " + "; ".join(problems)
        )
    return view


@dataclass(frozen=True)
class PairwiseItem:
    """One candidate performance available for pairwise comparison.

    ``item_ref`` is the real, non-blinded reference (e.g. ``"5602827182:8"``
    for game_id:participant_id) and is never shown to a reviewer -- only
    used internally to build presentations and to resolve tokens back to
    reality at export time.
    """
    item_ref: str
    features: Mapping


def make_pair_id(item_a: PairwiseItem, item_b: PairwiseItem) -> str:
    """Deterministic, order-independent id for an unordered pair."""
    refs = sorted((item_a.item_ref, item_b.item_ref))
    return sha256_hex(f"{refs[0]}|{refs[1]}")[:20]


@dataclass(frozen=True)
class PresentedPair:
    pair_id: str
    left_token: str
    right_token: str
    left_view: Mapping
    right_view: Mapping
    presentation_seed: str

    def to_dict(self) -> dict:
        return {
            "pair_id": self.pair_id,
            "left_token": self.left_token,
            "right_token": self.right_token,
            "left_view": dict(self.left_view),
            "right_view": dict(self.right_view),
            "presentation_seed": self.presentation_seed,
        }


def build_presentation(
        item_a: PairwiseItem, item_b: PairwiseItem,
        *, seed: str) -> tuple[PresentedPair, dict[str, str]]:
    """Build a blinded, randomized-order presentation for one pair.

    Returns ``(presentation, token_map)``. ``token_map`` (``{"left_token":
    ..., "right_token": ..., "left_ref": ..., "right_ref": ...}``) is
    private bookkeeping the caller must store separately from anything
    shown to a reviewer -- it is what makes :func:`export_for_training`
    possible later.

    Left/right order is decided by a seeded RNG keyed on ``seed`` and the
    pair's own (order-independent) id, so the same pair with the same seed
    always renders the same way -- reproducible, but only fair (roughly
    50/50 across many pairs) because the seed is fixed once per review
    round, not per item.
    """
    pair_id = make_pair_id(item_a, item_b)
    rng = random.Random(f"{seed}:{pair_id}")
    if rng.random() < 0.5:
        left_item, right_item = item_a, item_b
    else:
        left_item, right_item = item_b, item_a
    left_token = sha256_hex(f"{seed}:{pair_id}:left")[:12]
    right_token = sha256_hex(f"{seed}:{pair_id}:right")[:12]
    presentation = PresentedPair(
        pair_id=pair_id,
        left_token=left_token,
        right_token=right_token,
        left_view=redact_for_presentation(left_item.features),
        right_view=redact_for_presentation(right_item.features),
        presentation_seed=seed,
    )
    # Keep left/right explicit rather than a flat {token: ref} dict -- the
    # tokens are opaque hashes with no inherent left/right ordering, so a
    # naive "sort the two tokens" resolution at export time would silently
    # scramble which side actually won.
    token_map = {
        "left_token": left_token, "right_token": right_token,
        "left_ref": left_item.item_ref, "right_ref": right_item.item_ref,
    }
    return presentation, token_map


@dataclass(frozen=True)
class ReviewLabel:
    label_id: str
    pair_id: str
    reviewer_id: str
    choice: str
    confidence: float
    rationale_tags: tuple[str, ...]
    notes: str
    created_at: str
    presentation_seed: str

    def to_dict(self) -> dict:
        return {
            "label_id": self.label_id,
            "pair_id": self.pair_id,
            "reviewer_id": self.reviewer_id,
            "choice": self.choice,
            "confidence": self.confidence,
            "rationale_tags": list(self.rationale_tags),
            "notes": self.notes,
            "created_at": self.created_at,
            "presentation_seed": self.presentation_seed,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "ReviewLabel":
        return cls(
            label_id=data["label_id"],
            pair_id=data["pair_id"],
            reviewer_id=data["reviewer_id"],
            choice=data["choice"],
            confidence=float(data["confidence"]),
            rationale_tags=tuple(data.get("rationale_tags", ())),
            notes=data.get("notes", ""),
            created_at=data["created_at"],
            presentation_seed=data.get("presentation_seed", ""),
        )


def make_label(
        *, pair_id: str, reviewer_id: str, choice: str, confidence: float,
        rationale_tags: Iterable[str], notes: str = "",
        presentation_seed: str = "",
        now: Optional[datetime.datetime] = None) -> ReviewLabel:
    timestamp = (now or datetime.datetime.now(datetime.timezone.utc)).isoformat()
    label = ReviewLabel(
        label_id=sha256_hex(f"{pair_id}:{reviewer_id}:{timestamp}")[:24],
        pair_id=pair_id,
        reviewer_id=reviewer_id,
        choice=choice,
        confidence=float(confidence),
        rationale_tags=tuple(rationale_tags),
        notes=notes,
        created_at=timestamp,
        presentation_seed=presentation_seed,
    )
    validate_label(label)
    return label


def validate_label(label: ReviewLabel) -> None:
    if label.choice not in VALID_CHOICES:
        raise ReviewValidationError(f"Unknown choice '{label.choice}'")
    if not (0.0 <= label.confidence <= 1.0):
        raise ReviewValidationError("confidence must be within [0.0, 1.0]")
    if not label.rationale_tags:
        raise ReviewValidationError("at least one rationale tag is required")
    unknown_tags = set(label.rationale_tags) - set(VALID_RATIONALE_TAGS)
    if unknown_tags:
        raise ReviewValidationError(f"Unknown rationale tags: {sorted(unknown_tags)}")
    if not label.reviewer_id:
        raise ReviewValidationError("reviewer_id must not be empty")
    problems = scan_for_forbidden(label.to_dict())
    if problems:
        raise ReviewValidationError(
            "label contains disallowed data: " + "; ".join(problems)
        )


class ReviewLabelStore:
    """Append-only JSONL store for review labels.

    There is intentionally no update/delete method: a correction is recorded
    as a new label for the same ``pair_id`` from the same reviewer, and
    :func:`compute_agreement`/:func:`export_for_training` both use each
    reviewer's most recent label per pair.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.last_load_errors: list[str] = []

    def add_label(self, label: ReviewLabel) -> None:
        validate_label(label)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(label.to_dict(), sort_keys=True) + "\n")

    def iter_labels(self) -> Iterator[ReviewLabel]:
        self.last_load_errors = []
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                    label = ReviewLabel.from_dict(data)
                    validate_label(label)
                except (json.JSONDecodeError, KeyError, ValueError, ReviewValidationError) as exc:
                    self.last_load_errors.append(f"line {line_number}: {exc}")
                    continue
                yield label

    def all_labels(self) -> list[ReviewLabel]:
        return list(self.iter_labels())


def _latest_label_per_reviewer(labels: Sequence[ReviewLabel]) -> dict[tuple[str, str], ReviewLabel]:
    latest: dict[tuple[str, str], ReviewLabel] = {}
    for label in labels:
        key = (label.pair_id, label.reviewer_id)
        current = latest.get(key)
        if current is None or label.created_at >= current.created_at:
            latest[key] = label
    return latest


@dataclass
class AgreementReport:
    total_pairs_with_multiple_raters: int
    unanimous_pairs: int
    agreement_rate: float
    pairwise_cohens_kappa: Optional[float]
    per_choice_counts: dict[str, int]
    reviewer_counts: dict[str, int]

    def to_dict(self) -> dict:
        return {
            "total_pairs_with_multiple_raters": self.total_pairs_with_multiple_raters,
            "unanimous_pairs": self.unanimous_pairs,
            "agreement_rate": self.agreement_rate,
            "pairwise_cohens_kappa": self.pairwise_cohens_kappa,
            "per_choice_counts": dict(self.per_choice_counts),
            "reviewer_counts": dict(self.reviewer_counts),
        }


def _cohens_kappa(pairs: Sequence[tuple[str, str]]) -> Optional[float]:
    if not pairs:
        return None
    categories = sorted({a for a, _ in pairs} | {b for _, b in pairs})
    n = len(pairs)
    observed_agreement = sum(1 for a, b in pairs if a == b) / n
    expected_agreement = 0.0
    for category in categories:
        p_a = sum(1 for a, _ in pairs if a == category) / n
        p_b = sum(1 for _, b in pairs if b == category) / n
        expected_agreement += p_a * p_b
    if expected_agreement >= 1.0:
        return 1.0 if observed_agreement >= 1.0 else 0.0
    return (observed_agreement - expected_agreement) / (1 - expected_agreement)


def compute_agreement(labels: Sequence[ReviewLabel]) -> AgreementReport:
    latest = _latest_label_per_reviewer(labels)
    by_pair: dict[str, dict[str, ReviewLabel]] = {}
    per_choice_counts: dict[str, int] = {}
    reviewer_counts: dict[str, int] = {}
    for (pair_id, reviewer_id), label in latest.items():
        by_pair.setdefault(pair_id, {})[reviewer_id] = label
        per_choice_counts[label.choice] = per_choice_counts.get(label.choice, 0) + 1
        reviewer_counts[reviewer_id] = reviewer_counts.get(reviewer_id, 0) + 1

    multi_rater_pairs = {
        pair_id: reviewers for pair_id, reviewers in by_pair.items()
        if len(reviewers) >= 2
    }
    unanimous = sum(
        1 for reviewers in multi_rater_pairs.values()
        if len({label.choice for label in reviewers.values()}) == 1
    )
    total = len(multi_rater_pairs)
    agreement_rate = (unanimous / total) if total else 0.0

    all_reviewer_ids = sorted(reviewer_counts.keys())
    kappa: Optional[float] = None
    if len(all_reviewer_ids) == 2:
        rater_a, rater_b = all_reviewer_ids
        shared_pairs = [
            (reviewers[rater_a].choice, reviewers[rater_b].choice)
            for reviewers in by_pair.values()
            if rater_a in reviewers and rater_b in reviewers
        ]
        kappa = _cohens_kappa(shared_pairs)

    return AgreementReport(
        total_pairs_with_multiple_raters=total,
        unanimous_pairs=unanimous,
        agreement_rate=agreement_rate,
        pairwise_cohens_kappa=kappa,
        per_choice_counts=per_choice_counts,
        reviewer_counts=reviewer_counts,
    )


def export_for_training(
        labels: Sequence[ReviewLabel], token_maps: Mapping[str, Mapping[str, str]],
        *, on_missing_mapping: str = "raise") -> list[dict]:
    """De-blind labels into training-ready rows using the private token maps.

    ``token_maps`` is ``{pair_id: {"left_token", "right_token", "left_ref",
    "right_ref"}}`` as produced by :func:`build_presentation` at
    presentation time and persisted separately from anything
    reviewer-facing. Append-only corrections are collapsed to each
    reviewer's latest label for a pair before export; distinct reviewers
    remain separate rows so downstream training can weight confidence and
    reviewer agreement.
    """
    if on_missing_mapping not in ("raise", "skip"):
        raise ValueError("on_missing_mapping must be 'raise' or 'skip'")
    rows: list[dict] = []
    latest_labels = sorted(
        _latest_label_per_reviewer(labels).values(),
        key=lambda label: (label.pair_id, label.reviewer_id),
    )
    for label in latest_labels:
        mapping = token_maps.get(label.pair_id)
        if mapping is None:
            if on_missing_mapping == "raise":
                raise ReviewValidationError(
                    f"No token map for pair_id '{label.pair_id}'"
                )
            continue
        required = ("left_token", "right_token", "left_ref", "right_ref")
        if not all(key in mapping for key in required):
            if on_missing_mapping == "raise":
                raise ReviewValidationError(
                    f"Token map for pair_id '{label.pair_id}' is missing "
                    f"one of {required}"
                )
            continue
        left_ref = mapping["left_ref"]
        right_ref = mapping["right_ref"]
        if label.choice == "left":
            winner_ref, relation = left_ref, "left_preferred"
        elif label.choice == "right":
            winner_ref, relation = right_ref, "right_preferred"
        else:
            winner_ref, relation = None, label.choice
        rows.append({
            "pair_id": label.pair_id,
            "left_ref": left_ref,
            "right_ref": right_ref,
            "winner_ref": winner_ref,
            "relation": relation,
            "choice": label.choice,
            "confidence": label.confidence,
            "rationale_tags": list(label.rationale_tags),
            "reviewer_id": label.reviewer_id,
            "created_at": label.created_at,
        })
    return rows


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Blinded pairwise review utilities")
    sub = parser.add_subparsers(dest="command", required=True)

    present_parser = sub.add_parser(
        "present", help="Build a blinded presentation for two feature JSON files",
    )
    present_parser.add_argument("item_a_ref")
    present_parser.add_argument("item_a_features_path")
    present_parser.add_argument("item_b_ref")
    present_parser.add_argument("item_b_features_path")
    present_parser.add_argument("--seed", required=True)
    present_parser.add_argument("--token-map-out", required=True)

    label_parser = sub.add_parser("label", help="Append a review label")
    label_parser.add_argument("labels_path")
    label_parser.add_argument("--pair-id", required=True)
    label_parser.add_argument("--reviewer-id", required=True)
    label_parser.add_argument("--choice", required=True, choices=VALID_CHOICES)
    label_parser.add_argument("--confidence", required=True, type=float)
    label_parser.add_argument("--tags", required=True, help="Comma-separated rationale tags")
    label_parser.add_argument("--notes", default="")
    label_parser.add_argument("--seed", default="")

    agreement_parser = sub.add_parser("agreement", help="Compute inter-rater agreement")
    agreement_parser.add_argument("labels_path")

    export_parser = sub.add_parser("export", help="Export labels for training")
    export_parser.add_argument("labels_path")
    export_parser.add_argument("token_map_path")

    args = parser.parse_args()

    if args.command == "present":
        item_a = PairwiseItem(
            args.item_a_ref,
            json.loads(Path(args.item_a_features_path).read_text(encoding="utf-8")),
        )
        item_b = PairwiseItem(
            args.item_b_ref,
            json.loads(Path(args.item_b_features_path).read_text(encoding="utf-8")),
        )
        presentation, token_map = build_presentation(item_a, item_b, seed=args.seed)
        Path(args.token_map_out).write_text(
            json.dumps({presentation.pair_id: token_map}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(json.dumps(presentation.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "label":
        store = ReviewLabelStore(Path(args.labels_path))
        label = make_label(
            pair_id=args.pair_id, reviewer_id=args.reviewer_id, choice=args.choice,
            confidence=args.confidence,
            rationale_tags=[tag.strip() for tag in args.tags.split(",") if tag.strip()],
            notes=args.notes, presentation_seed=args.seed,
        )
        store.add_label(label)
        print(f"Appended label {label.label_id}")
        return 0

    if args.command == "agreement":
        store = ReviewLabelStore(Path(args.labels_path))
        report = compute_agreement(store.all_labels())
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        if store.last_load_errors:
            print(f"({len(store.last_load_errors)} malformed lines skipped)")
        return 0

    if args.command == "export":
        store = ReviewLabelStore(Path(args.labels_path))
        token_maps = json.loads(Path(args.token_map_path).read_text(encoding="utf-8"))
        rows = export_for_training(store.all_labels(), token_maps, on_missing_mapping="skip")
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
