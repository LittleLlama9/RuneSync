"""Dependency-free DAEMON Score v2 runtime scorer.

Stdlib-only (`math`, `dataclasses`, `typing`) by design -- this is the code
path a packaged RuneSync build actually executes, so it must never import
`numpy`/`scipy`/`sklearn` even if those happen to be installed in a
development environment. Given a verified `score_v2.artifact.Artifact` and
one game's `score_features.compute_feature_set(...)` output, it scores one
participant at a time, purely from that participant's own feature block
(so it is naturally invariant to participant iteration order -- there is no
notion of "the other nine players" here beyond what `score_features.py`
already baked into each participant's block).

Tier routing (`select_artifact`) is an exact-key lookup only: it never
substitutes a different evidence tier's artifact for a missing one.
"Fallback/shrinkage" is expressed on the artifact itself (`fallback`
metadata set at training/export time), not invented at routing time --
see `score_v2.artifact.Artifact.fallback` and the corresponding
`docs/SCORE_V2_MODELS.md` section.

`ScoreResult.confidence` is a per-participant number (how much evidence
this ONE participant's score itself rests on). It is intentionally NOT
the same thing as a rank confidence, which requires knowing every other
participant's score too -- that only exists at `score_game` level, as
`RankedScoreResult.rank_confidence`, computed from real score gaps and
score-interval overlap between neighbors in the sorted order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Optional

from score_v2.artifact import Artifact
from score_v2.feature_spec import extract_feature_vector, resolve_role


class ArtifactUnavailableError(Exception):
    """Raised when no artifact is registered for a requested evidence tier."""


class EvidenceTierMismatchError(Exception):
    """Raised when `game_features["evidence_source"]` != `artifact.evidence_source`."""


def select_artifact(artifacts: Mapping[str, Artifact], evidence_source: str) -> Artifact:
    """Look up the artifact for `evidence_source`, with no implicit substitution.

    `artifacts` is caller-provided (e.g. `{"match_v5": Artifact(...), ...}`)
    -- this module never loads artifacts from disk itself or guesses a
    path, keeping runtime routing a pure function of what the caller
    explicitly registered.
    """
    artifact = artifacts.get(evidence_source)
    if artifact is None:
        raise ArtifactUnavailableError(
            f"No artifact registered for evidence tier '{evidence_source}'; "
            "score_v2 never substitutes a different tier's artifact "
            "implicitly -- train/export one for this tier (optionally with "
            "explicit fallback/shrinkage metadata) before routing games of "
            "this tier."
        )
    if artifact.evidence_source != evidence_source:
        raise ArtifactUnavailableError(
            f"Artifact registered under key '{evidence_source}' declares "
            f"evidence_source={artifact.evidence_source!r}; refusing to use "
            "a mismatched artifact"
        )
    return artifact


@dataclass(frozen=True)
class ScoreResult:
    participant_id: int
    evidence_source: str
    role: str
    score: float
    raw_linear_score: float
    confidence: float
    score_interval: tuple[float, float]
    present_feature_count: int
    total_feature_count: int
    missing_features: tuple[str, ...]
    abstain: bool
    abstain_reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "participant_id": self.participant_id,
            "evidence_source": self.evidence_source,
            "role": self.role,
            "score": self.score,
            "raw_linear_score": self.raw_linear_score,
            "confidence": self.confidence,
            "score_interval": list(self.score_interval),
            "present_feature_count": self.present_feature_count,
            "total_feature_count": self.total_feature_count,
            "missing_features": list(self.missing_features),
            "abstain": self.abstain,
            "abstain_reasons": list(self.abstain_reasons),
        }


def score_participant(
        artifact: Artifact, game_features: Mapping, participant_id) -> ScoreResult:
    """Score one participant against one already-loaded, verified artifact.

    `game_features` is the whole dict returned by
    `score_features.compute_feature_set` (or persisted via
    `HistoryStore.save_feature_set`/read back via `get_feature_set`) --
    NOT a single participant's block -- so this function can also read the
    game-level `abstain`/`abstain_reason`/`chosen_source_completeness`
    fields already computed there. Raises `EvidenceTierMismatchError` if
    `game_features["evidence_source"]` does not exactly equal
    `artifact.evidence_source` -- an artifact must never score evidence
    from a tier it was not built for, even if a caller bypasses
    `select_artifact`/`score_game` and calls this directly.
    """
    artifact.verify_content_hash()
    game_evidence_source = game_features.get("evidence_source")
    if game_evidence_source != artifact.evidence_source:
        raise EvidenceTierMismatchError(
            f"game_features declares evidence_source={game_evidence_source!r} "
            f"but artifact is for evidence_source={artifact.evidence_source!r}; "
            "refusing to score a mismatched evidence tier"
        )
    participants = game_features.get("participants") or {}
    block = participants.get(str(participant_id))
    if block is None:
        raise ValueError(f"No feature block for participant_id={participant_id!r}")

    specs = [coefficient.spec for coefficient in artifact.coefficients]
    values = extract_feature_vector(block, specs=specs)
    role = resolve_role(block)

    total = len(artifact.coefficients)
    missing_names = [name for name, value in values.items() if not value.present]
    present_count = total - len(missing_names)
    present_fraction = (present_count / total) if total else 0.0

    linear = artifact.intercept
    for coefficient in artifact.coefficients:
        value = values[coefficient.spec.name]
        if not value.present:
            # Missing features contribute nothing (a neutral 0 in
            # normalized space) -- the resulting confidence penalty below
            # is how "missing evidence" is honestly represented, rather
            # than silently imputing a guessed value that looks confident.
            continue
        normalized = (value.transformed - coefficient.robust_center) / coefficient.robust_scale
        linear += coefficient.coefficient * normalized

    role_calibration = (
        artifact.role_calibration.get(role)
        or artifact.role_calibration.get("unknown")
    )
    role_offset = role_calibration.offset if role_calibration else 0.0
    adjusted = linear - role_offset

    midpoint = float(artifact.score_calibration["midpoint"])
    scale = float(artifact.score_calibration["scale"])
    clip_min = float(artifact.score_calibration["clip_min"])
    clip_max = float(artifact.score_calibration["clip_max"])
    half_range = (clip_max - clip_min) / 2.0
    raw_score = (
        midpoint + half_range * math.tanh(adjusted / scale) if scale > 0 else midpoint
    )

    missing_fraction = 1.0 - present_fraction
    missing_penalty = float(artifact.confidence_params.get("missing_feature_penalty", 0.5))
    evidence_quality_weight = float(
        artifact.confidence_params.get("evidence_quality_weight", 0.5)
    )
    evidence_quality = float(game_features.get("chosen_source_completeness") or 0.0)
    base_confidence = max(0.0, 1.0 - missing_penalty * missing_fraction)
    quality_component = (
        (1.0 - evidence_quality_weight) + evidence_quality_weight * evidence_quality
    )
    confidence = max(0.0, min(1.0, base_confidence * quality_component))

    # Uncertainty shrinkage toward the semantic midpoint (50): the less we
    # trust this number, the closer it is pulled to "average", never
    # toward an arbitrary extreme.
    shrunk_score = midpoint + (raw_score - midpoint) * confidence
    shrunk_score = max(clip_min, min(clip_max, shrunk_score))

    min_half = float(artifact.confidence_params.get("interval_min_half_width", 3.0))
    max_half = float(artifact.confidence_params.get("interval_max_half_width", 40.0))
    half_width = max_half - confidence * (max_half - min_half)
    interval = (
        max(clip_min, shrunk_score - half_width),
        min(clip_max, shrunk_score + half_width),
    )

    abstain_reasons: list[str] = []
    if game_features.get("abstain"):
        abstain_reasons.append(str(game_features.get("abstain_reason") or "short_game"))
    min_present_fraction = float(
        artifact.abstention_params.get("min_present_feature_fraction", 0.3)
    )
    if present_fraction < min_present_fraction:
        abstain_reasons.append("insufficient_features")
    min_confidence_to_report = float(
        artifact.abstention_params.get("min_confidence_to_report", 0.15)
    )
    if confidence < min_confidence_to_report:
        abstain_reasons.append("low_confidence")

    return ScoreResult(
        participant_id=int(participant_id),
        evidence_source=artifact.evidence_source,
        role=role,
        score=round(shrunk_score, 2),
        raw_linear_score=round(adjusted, 6),
        confidence=round(confidence, 4),
        score_interval=(round(interval[0], 2), round(interval[1], 2)),
        present_feature_count=present_count,
        total_feature_count=total,
        missing_features=tuple(sorted(missing_names)),
        abstain=bool(abstain_reasons),
        abstain_reasons=tuple(abstain_reasons),
    )


def _pairwise_rank_confidence(a: ScoreResult, b: ScoreResult) -> float:
    """How confidently `a` and `b` are correctly ordered relative to each
    other, given both their score gap and their score-interval overlap.

    1.0 = the gap between them fully clears both intervals' half-widths
    (confidently separated); 0.0 = the gap is fully swallowed by their
    combined uncertainty (indistinguishable). This is a genuine two-item
    comparison -- it cannot be computed from either participant's own
    `confidence` in isolation.
    """
    gap = abs(a.score - b.score)
    a_half = (a.score_interval[1] - a.score_interval[0]) / 2.0
    b_half = (b.score_interval[1] - b.score_interval[0]) / 2.0
    combined_spread = a_half + b_half
    if combined_spread <= 0.0:
        return 1.0 if gap > 0.0 else 0.0
    return max(0.0, min(1.0, gap / combined_spread))


@dataclass(frozen=True)
class RankedScoreResult:
    """One participant's score plus its position within the full game.

    `rank` is 1-indexed, highest score first (ties broken by
    `participant_id` for determinism). `rank_confidence` is a genuine
    group-level measure -- the minimum pairwise rank confidence against
    this participant's immediate neighbors in the sorted order (1.0 for
    the sole participant in a group of one) -- distinct from
    `result.confidence`, which reflects only this participant's own
    evidence completeness.
    """

    rank: int
    result: ScoreResult
    rank_confidence: float

    def to_dict(self) -> dict:
        payload = self.result.to_dict()
        payload["rank"] = self.rank
        payload["rank_confidence"] = self.rank_confidence
        return payload


def score_game(
        artifacts: Mapping[str, Artifact], game_features: Mapping,
) -> dict[int, RankedScoreResult]:
    """Score every participant in `game_features` using its own evidence tier.

    Convenience wrapper around `select_artifact` + `score_participant`;
    raises `ArtifactUnavailableError` if no artifact is registered for the
    game's `evidence_source` (never falls back implicitly). Also computes
    a genuine group-level `rank_confidence` per participant -- see
    `RankedScoreResult`.
    """
    evidence_source = game_features.get("evidence_source")
    artifact = select_artifact(artifacts, evidence_source)
    participants = game_features.get("participants") or {}
    results = {
        int(pid): score_participant(artifact, game_features, pid)
        for pid in participants
    }

    ordered = sorted(results.items(), key=lambda item: (-item[1].score, item[0]))
    ranked: dict[int, RankedScoreResult] = {}
    for index, (pid, result) in enumerate(ordered):
        neighbor_confidences = []
        if index > 0:
            neighbor_confidences.append(
                _pairwise_rank_confidence(result, ordered[index - 1][1])
            )
        if index < len(ordered) - 1:
            neighbor_confidences.append(
                _pairwise_rank_confidence(result, ordered[index + 1][1])
            )
        rank_confidence = min(neighbor_confidences) if neighbor_confidences else 1.0
        ranked[pid] = RankedScoreResult(
            rank=index + 1, result=result, rank_confidence=round(rank_confidence, 4),
        )
    return ranked
