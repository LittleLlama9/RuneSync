"""Grouped train/validation/test split assignment and leakage checks.

Splits are assigned over *connected components* of matches and players (via
union-find), never over raw entry counts: if a player appears in two
matches, or a match contributes two manifest entries (e.g. an
``lcu_timeline`` entry and a ``match_v5`` entry for the same game), every
entry touching that component lands in exactly one split. Assignment is
deterministic given a seed: the same seed and inputs always produce the same
split map, and changing the seed reshuffles components without needing any
mutable state.

``check_leakage`` is an independent second pass that does not assume its
input came from :func:`assign_splits` -- it re-derives the hard constraints
(no player or match may span splits) from the manifest entries themselves,
so it also catches a manually-edited or corrupted split assignment.
"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from corpus.manifest import ManifestEntry

DEFAULT_SPLIT_RATIOS: dict[str, float] = {
    "train": 0.7,
    "validation": 0.15,
    "test": 0.15,
}

_RATIO_EPSILON = 1e-6


class SplitConfigError(Exception):
    """Raised when a :class:`SplitConfig` is internally inconsistent."""


class SplitLeakageError(Exception):
    """Raised by :func:`assign_splits_strict` when hard leakage is found."""


@dataclass(frozen=True)
class SplitConfig:
    seed: str
    ratios: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_SPLIT_RATIOS)
    )
    version: str = "v1"

    def __post_init__(self):
        if not self.ratios:
            raise SplitConfigError("ratios must not be empty")
        total = sum(self.ratios.values())
        if abs(total - 1.0) > _RATIO_EPSILON:
            raise SplitConfigError(f"ratios must sum to 1.0, got {total}")
        for name, value in self.ratios.items():
            if value < 0:
                raise SplitConfigError(f"ratio for '{name}' must be >= 0")


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def _ensure(self, key: str) -> None:
        if key not in self._parent:
            self._parent[key] = key

    def find(self, key: str) -> str:
        self._ensure(key)
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:
            self._parent[key], key = root, self._parent[key]
        return root

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return
        # Deterministic tie-break so the resulting root does not depend on
        # call order -- always keep the lexicographically smaller key as
        # root.
        if root_b < root_a:
            root_a, root_b = root_b, root_a
        self._parent[root_b] = root_a


def _component_bucket(seed: str, version: str, component_root: str) -> float:
    digest = hashlib.sha256(
        f"{seed}:{version}:{component_root}".encode("utf-8")
    ).hexdigest()
    return int(digest, 16) / float(1 << 256)


def assign_splits(
        entries: Sequence[ManifestEntry], config: SplitConfig) -> dict[str, str]:
    """Return ``{entry_id: split_name}`` with players/matches never split.

    Deterministic: the same ``entries`` (any order) and the same ``config``
    always produce the same assignment.
    """
    uf = _UnionFind()
    for entry in entries:
        match_key = f"match:{entry.leakage.match_group_key}"
        uf._ensure(match_key)
        for player_key in entry.leakage.player_group_keys:
            uf.union(match_key, f"player:{player_key}")

    # Sorted split names give a stable, unambiguous cumulative-threshold
    # order regardless of the order ratios were supplied in.
    ordered_splits = sorted(config.ratios.keys())
    thresholds: list[tuple[str, float]] = []
    cumulative = 0.0
    for name in ordered_splits:
        cumulative += config.ratios[name]
        thresholds.append((name, cumulative))

    def split_for_bucket(bucket: float) -> str:
        for name, threshold in thresholds:
            if bucket < threshold:
                return name
        return thresholds[-1][0]

    component_root_by_entry: dict[str, str] = {}
    for entry in entries:
        root = uf.find(f"match:{entry.leakage.match_group_key}")
        component_root_by_entry[entry.entry_id] = root

    split_by_root: dict[str, str] = {}
    assignment: dict[str, str] = {}
    for entry in entries:
        root = component_root_by_entry[entry.entry_id]
        if root not in split_by_root:
            bucket = _component_bucket(config.seed, config.version, root)
            split_by_root[root] = split_for_bucket(bucket)
        assignment[entry.entry_id] = split_by_root[root]
    return assignment


@dataclass
class LeakageReport:
    hard_violations: list[str]
    warnings: list[str]
    per_split_counts: dict[str, int]

    def is_clean(self) -> bool:
        return not self.hard_violations

    def to_dict(self) -> dict:
        return {
            "hard_violations": list(self.hard_violations),
            "warnings": list(self.warnings),
            "per_split_counts": dict(self.per_split_counts),
        }


def _parse_game_creation_date(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def check_leakage(
        entries: Sequence[ManifestEntry], assignments: Mapping[str, str],
        *, temporal_window_seconds: int = 300) -> LeakageReport:
    """Independently verify an assignment against the hard/soft rules.

    Hard violations (player/match/duplicate-content spanning splits) make
    the corpus unsafe to train or evaluate on and should block downstream
    use. Warnings flag coverage concentration (a champion/region/rank/patch
    value confined to one split despite appearing elsewhere) or
    session-adjacent matches split apart in time, which are worth a human's
    attention but are not automatically disqualifying.
    """
    hard: list[str] = []
    warnings: list[str] = []

    by_entry_id = {entry.entry_id: entry for entry in entries}
    missing = [eid for eid in by_entry_id if eid not in assignments]
    if missing:
        raise SplitLeakageError(
            f"assignments missing for entries: {sorted(missing)}"
        )

    # Hard: player identity must never span splits.
    splits_by_player: dict[str, set[str]] = {}
    for entry in entries:
        split = assignments[entry.entry_id]
        for player_key in entry.leakage.player_group_keys:
            splits_by_player.setdefault(player_key, set()).add(split)
    for player_key, splits in splits_by_player.items():
        if len(splits) > 1:
            hard.append(
                f"player group '{player_key}' spans splits {sorted(splits)}"
            )

    # Hard: a match (by group key) must never span splits.
    splits_by_match: dict[str, set[str]] = {}
    for entry in entries:
        splits_by_match.setdefault(entry.leakage.match_group_key, set()).add(
            assignments[entry.entry_id]
        )
    for match_key, splits in splits_by_match.items():
        if len(splits) > 1:
            hard.append(
                f"match group '{match_key}' spans splits {sorted(splits)}"
            )

    # Hard: repeated/duplicate content (identical content_hash) must never
    # span splits -- this catches the same evidence re-ingested under a
    # different game id or source label.
    splits_by_hash: dict[str, set[str]] = {}
    for entry in entries:
        splits_by_hash.setdefault(entry.content_hash, set()).add(
            assignments[entry.entry_id]
        )
    for content_hash, splits in splits_by_hash.items():
        if len(splits) > 1:
            hard.append(
                f"repeated content hash '{content_hash[:12]}...' spans "
                f"splits {sorted(splits)}"
            )

    # Soft: concentration of a champion/region/rank/patch value into a
    # single split when it appears in multiple entries overall.
    def _concentration_warnings(attr_name: str, getter) -> None:
        splits_by_value: dict[str, set[str]] = {}
        counts: dict[str, int] = {}
        for entry in entries:
            value = getter(entry)
            if value is None:
                continue
            splits_by_value.setdefault(value, set()).add(assignments[entry.entry_id])
            counts[value] = counts.get(value, 0) + 1
        for value, splits in splits_by_value.items():
            if counts[value] >= 2 and len(splits) == 1:
                warnings.append(
                    f"{attr_name} '{value}' appears {counts[value]} times "
                    f"but only in split '{next(iter(splits))}'"
                )

    _concentration_warnings("champion", lambda e: e.leakage.champion)
    _concentration_warnings("region", lambda e: e.leakage.region)
    _concentration_warnings("rank_tier", lambda e: e.leakage.rank_tier)
    _concentration_warnings("patch", lambda e: e.leakage.patch)

    # Soft: temporally-adjacent matches split apart (possible remake/
    # re-queue pairs that are not already grouped by shared player/match).
    timestamped = [
        (entry, _parse_game_creation_date(entry.game_metadata.game_creation_date))
        for entry in entries
    ]
    timestamped = [(e, t) for e, t in timestamped if t is not None]
    timestamped.sort(key=lambda pair: pair[1])
    for index in range(len(timestamped) - 1):
        entry_a, time_a = timestamped[index]
        entry_b, time_b = timestamped[index + 1]
        if entry_a.leakage.match_group_key == entry_b.leakage.match_group_key:
            continue
        delta = abs((time_b - time_a).total_seconds())
        if delta <= temporal_window_seconds:
            split_a = assignments[entry_a.entry_id]
            split_b = assignments[entry_b.entry_id]
            if split_a != split_b:
                warnings.append(
                    f"'{entry_a.entry_id}' and '{entry_b.entry_id}' are "
                    f"{delta:.0f}s apart but assigned to different splits "
                    f"('{split_a}' vs '{split_b}')"
                )

    per_split_counts: dict[str, int] = {}
    for split in assignments.values():
        per_split_counts[split] = per_split_counts.get(split, 0) + 1

    return LeakageReport(
        hard_violations=hard, warnings=warnings, per_split_counts=per_split_counts,
    )


def assign_splits_strict(
        entries: Sequence[ManifestEntry], config: SplitConfig,
        *, temporal_window_seconds: int = 300,
        ) -> tuple[dict[str, str], LeakageReport]:
    """Assign splits and raise :class:`SplitLeakageError` on hard violations."""
    assignments = assign_splits(entries, config)
    report = check_leakage(
        entries, assignments, temporal_window_seconds=temporal_window_seconds,
    )
    if not report.is_clean():
        raise SplitLeakageError("; ".join(report.hard_violations))
    return assignments, report


def _cli() -> int:
    import argparse
    import json

    from corpus.manifest import CorpusManifest

    parser = argparse.ArgumentParser(description="Corpus split assignment utilities")
    parser.add_argument("manifest_path")
    parser.add_argument("--seed", required=True)
    parser.add_argument("--train", type=float, default=DEFAULT_SPLIT_RATIOS["train"])
    parser.add_argument(
        "--validation", type=float, default=DEFAULT_SPLIT_RATIOS["validation"]
    )
    parser.add_argument("--test", type=float, default=DEFAULT_SPLIT_RATIOS["test"])
    args = parser.parse_args()

    manifest = CorpusManifest.load(args.manifest_path)
    entries = manifest.to_list()
    config = SplitConfig(
        seed=args.seed,
        ratios={"train": args.train, "validation": args.validation, "test": args.test},
    )
    try:
        assignments, report = assign_splits_strict(entries, config)
    except SplitLeakageError as exc:
        print(f"LEAKAGE: {exc}")
        return 1
    print(json.dumps(
        {"assignments": assignments, "report": report.to_dict()},
        indent=2, sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
