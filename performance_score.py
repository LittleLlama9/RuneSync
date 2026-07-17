"""DAEMON Score routing plus the retained v1 heuristic scorer."""

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from score_features import AGGREGATE, SOURCE_PRIORITY
from score_v2.artifact import (
    Artifact,
    ArtifactIntegrityError,
    ArtifactValidationError,
)
from score_v2.coaching import CoachingResult, build_coaching, build_observations
from score_v2.leakage import OutcomeLeakageError
from score_v2.runtime import (
    ArtifactUnavailableError,
    EvidenceTierMismatchError,
    RankedScoreResult,
    score_game,
)


SCORING_MODEL_VERSION = 1
SCORE_V2_MODEL_VERSION = 2

COMPONENT_WEIGHTS = {
    "lane": {
        "combat": 0.35, "economy": 0.25, "objectives": 0.15,
        "vision": 0.10, "teamplay": 0.15,
    },
    "jungle": {
        "combat": 0.30, "economy": 0.12, "objectives": 0.28,
        "vision": 0.12, "teamplay": 0.18,
    },
    "support": {
        "combat": 0.25, "economy": 0.03, "objectives": 0.15,
        "vision": 0.30, "teamplay": 0.27,
    },
    "unknown": {
        "combat": 0.30, "economy": 0.20, "objectives": 0.20,
        "vision": 0.15, "teamplay": 0.15,
    },
}


class ScoreRoutingError(Exception):
    """Raised when a configured Score v2 route is unsafe or inconsistent."""


@dataclass(frozen=True)
class RoutedScoreRun:
    """One complete Score v2 result ready for immutable persistence."""

    model_version: int
    artifact_model_version: str
    feature_version: str
    evidence_source: str
    calibration_version: str
    model_artifact_hash: str
    model_family: str
    scores: tuple[dict, ...]
    confidence: Mapping


class ScoreRouter:
    """Route feature sets through exact-tier Score v2 artifacts.

    An empty router is the normal installed state until production artifacts
    pass the replacement gates. It never substitutes an artifact from another
    evidence tier and never reads match outcome fields; v1 remains available
    through `score_match` independently.
    """

    def __init__(
            self, artifacts: Optional[Mapping[str, Artifact]] = None, *,
            allow_development_artifacts: bool = False):
        self._artifacts: dict[str, Artifact] = {}
        for source, artifact in (artifacts or {}).items():
            artifact.verify_content_hash()
            artifact.validate()
            if source != artifact.evidence_source:
                raise ScoreRoutingError(
                    f"Score v2 artifact registered for {source!r} declares "
                    f"{artifact.evidence_source!r}"
                )
            if source not in SOURCE_PRIORITY:
                raise ScoreRoutingError(f"Unknown Score v2 evidence source {source!r}")
            if not artifact.production_ready and not allow_development_artifacts:
                raise ScoreRoutingError(
                    f"Score v2 artifact for {source!r} is not production-ready"
                )
            self._artifacts[source] = artifact

    @property
    def enabled(self) -> bool:
        return bool(self._artifacts)

    @property
    def registered_sources(self) -> tuple[str, ...]:
        return tuple(
            source for source in SOURCE_PRIORITY if source in self._artifacts
        )

    def select_source(
            self, available_sources: Iterable[str],
            source_quality: Optional[Mapping[str, float]] = None) -> Optional[str]:
        available = set(available_sources)
        candidates = [
            source for source in SOURCE_PRIORITY
            if source in available and source in self._artifacts
        ]
        if not candidates:
            return None
        quality = source_quality or {}
        priority = {
            source: -index for index, source in enumerate(SOURCE_PRIORITY)
        }
        return max(
            candidates,
            key=lambda source: (
                -1.0 if source == AGGREGATE
                else float(quality.get(source, 1.0)),
                priority[source],
            ),
        )

    def score_feature_set(
            self, game_features: Mapping,
            evidence: Iterable[Mapping] = (), *,
            local_participant_id: Optional[int] = None,
            recent_local_features: Sequence[Mapping] = ()) -> RoutedScoreRun:
        source = game_features.get("evidence_source")
        artifact = self._artifacts.get(source)
        if artifact is None:
            raise ScoreRoutingError(
                f"No Score v2 artifact registered for evidence tier {source!r}"
            )
        feature_version = game_features.get("feature_version")
        if feature_version != artifact.feature_version:
            raise ScoreRoutingError(
                f"Feature set version {feature_version!r} does not match artifact "
                f"feature version {artifact.feature_version!r}"
            )
        try:
            ranked = score_game(self._artifacts, game_features)
        except (
                ArtifactIntegrityError, ArtifactValidationError,
                ArtifactUnavailableError, EvidenceTierMismatchError,
                OutcomeLeakageError, KeyError, TypeError, ValueError) as exc:
            raise ScoreRoutingError(
                f"Score v2 could not score {source!r} evidence: {exc}"
            ) from exc
        evidence_rows = tuple(dict(row) for row in evidence)
        participants = game_features.get("participants") or {}
        scores = []
        for participant_id in sorted(ranked):
            block = participants.get(str(participant_id)) or {}
            if participant_id == local_participant_id:
                coaching = build_coaching(
                    block, participant_id, evidence_rows,
                    artifact.evidence_source,
                    ranked[participant_id].result.confidence,
                    float(game_features.get("chosen_source_completeness") or 0.0),
                    ranked[participant_id].result.abstain,
                    ranked[participant_id].result.abstain_reasons,
                    recent_local_features,
                )
            else:
                coaching = CoachingResult(
                    observations=build_observations(
                        block, participant_id, evidence_rows,
                        artifact.evidence_source,
                    ),
                    eligible=False,
                    primary_focus=None,
                    challenges=(),
                    recurring_patterns=(),
                    withheld_reasons=("Coaching is generated only for the local player.",),
                )
            scores.append(
                self._storage_score(
                    ranked[participant_id], evidence_rows, coaching,
                )
            )
        scores = tuple(scores)
        abstained = [
            score["participant_id"] for score in scores if score["abstain"]
        ]
        return RoutedScoreRun(
            model_version=SCORE_V2_MODEL_VERSION,
            artifact_model_version=artifact.model_version,
            feature_version=artifact.feature_version,
            evidence_source=artifact.evidence_source,
            calibration_version=artifact.calibration_version,
            model_artifact_hash=artifact.content_hash,
            model_family=artifact.model_family,
            scores=scores,
            confidence={
                "score_version": SCORE_V2_MODEL_VERSION,
                "artifact_model_version": artifact.model_version,
                "model_family": artifact.model_family,
                "evidence_source": artifact.evidence_source,
                "feature_version": artifact.feature_version,
                "calibration_version": artifact.calibration_version,
                "production_ready": artifact.production_ready,
                "chosen_source_completeness": game_features.get(
                    "chosen_source_completeness",
                ),
                "abstained_participant_ids": abstained,
            },
        )

    @staticmethod
    def _storage_score(
            ranked: RankedScoreResult,
            evidence: tuple[Mapping, ...],
            coaching: CoachingResult) -> dict:
        result = ranked.result
        participant_evidence = [
            dict(row) for row in evidence
            if row.get("participant_id") in (None, result.participant_id)
        ]
        return {
            "participant_id": result.participant_id,
            "model_version": SCORE_V2_MODEL_VERSION,
            "total_score": result.score,
            "match_rank": ranked.rank,
            "components": {},
            "observations": list(coaching.observations),
            "evidence": participant_evidence,
            "score_low": result.score_interval[0],
            "score_high": result.score_interval[1],
            "participant_confidence": result.confidence,
            "rank_confidence": ranked.rank_confidence,
            "abstain": result.abstain,
            "abstain_reasons": list(result.abstain_reasons),
            "coaching_eligible": coaching.eligible,
            "coaching": coaching.to_dict(),
        }


def load_score_v2_artifacts(
        directory, *, require_production_ready: bool = True) -> dict[str, Artifact]:
    """Load exact-tier artifacts from `<directory>/<source>.json`.

    A missing directory or missing tier file is normal. A present but invalid
    artifact raises rather than silently leaving a route partially configured.
    """
    root = Path(directory)
    if not root.exists():
        return {}
    if not root.is_dir():
        raise ScoreRoutingError(f"Score v2 artifact path is not a directory: {root}")
    artifacts = {}
    for source in SOURCE_PRIORITY:
        path = root / f"{source}.json"
        if not path.exists():
            continue
        try:
            artifact = Artifact.load(path)
        except (
                OSError, KeyError, TypeError, ValueError,
                ArtifactIntegrityError, ArtifactValidationError) as exc:
            raise ScoreRoutingError(
                f"Could not load Score v2 artifact {path.name}: {exc}"
            ) from exc
        if artifact.evidence_source != source:
            raise ScoreRoutingError(
                f"Artifact {path.name} declares evidence tier "
                f"{artifact.evidence_source!r}"
            )
        if require_production_ready and not artifact.production_ready:
            raise ScoreRoutingError(
                f"Artifact {path.name} is not production-ready"
            )
        artifacts[source] = artifact
    return artifacts


def _safe_div(numerator, denominator) -> float:
    return float(numerator or 0) / max(float(denominator or 0), 1.0)


def _percentiles(values: list[float], lower_is_better: bool = False) -> list[float]:
    """Return midrank percentiles so ties receive identical deterministic values."""
    if len(values) <= 1:
        return [50.0 for _ in values]
    out = []
    for value in values:
        below = sum(1 for other in values if other < value)
        equal = sum(1 for other in values if other == value)
        rank = below + (equal - 1) / 2
        percentile = rank * 100 / (len(values) - 1)
        out.append(100 - percentile if lower_is_better else percentile)
    return out


def _role_bucket(role: str) -> str:
    if role == "jungle":
        return "jungle"
    if role == "support":
        return "support"
    if role in {"top", "mid", "bot"}:
        return "lane"
    return "unknown"


def _component_observations(player: dict, components: dict, all_players: list[dict]) -> list[str]:
    labels = {
        "combat": "Combat", "economy": "Economy", "objectives": "Objectives",
        "vision": "Vision", "teamplay": "Survival/teamplay",
    }
    strongest = max(components, key=components.get)
    weakest = min(components, key=components.get)
    spread = components[strongest] - components[weakest]
    observations = []
    if spread >= 0.1:
        observations.extend([
            f"{labels[strongest]} was the strongest component.",
            f"{labels[weakest]} ranked lowest among the score components.",
        ])
    else:
        observations.append("Score components were evenly balanced.")

    facts = (
        ("damage_to_champions", "Highest champion damage in the match."),
        ("vision_score", "Highest vision score in the match."),
        ("damage_to_objectives", "Highest objective damage in the match."),
    )
    for key, text in facts:
        value = player.get(key, 0)
        leaders = [
            p for p in all_players
            if p.get(key, 0) == max(x.get(key, 0) for x in all_players)
        ]
        if value > 0 and len(leaders) == 1 and leaders[0] is player:
            observations.append(text)

    team = [p for p in all_players if p["team_id"] == player["team_id"]]
    team_kills = sum(p["kills"] for p in team)
    participation = _safe_div(player["kills"] + player["assists"], team_kills)
    participation_values = [
        _safe_div(p["kills"] + p["assists"], team_kills) for p in team
    ]
    if team_kills > 0 and participation_values.count(participation) == 1 \
            and participation == max(participation_values):
        observations.append("Highest kill participation on the team.")
    return observations[:4]


def score_match(participants: list[dict], duration_seconds: int) -> list[dict]:
    """Score 10 normalized participants and assign deterministic ranks 1-10."""
    if len(participants) != 10:
        raise ValueError("DAEMON Score requires exactly 10 participants")
    minutes = max(float(duration_seconds) / 60.0, 1.0)

    team_totals = defaultdict(lambda: defaultdict(float))
    for player in participants:
        totals = team_totals[player["team_id"]]
        totals["kills"] += player["kills"]
        totals["damage"] += player["damage_to_champions"]
        totals["objectives"] += player["damage_to_objectives"]
        totals["turrets"] += player["damage_to_turrets"]

    metrics = defaultdict(list)
    raw_rows = []
    for player in participants:
        totals = team_totals[player["team_id"]]
        row = {
            "kill_participation": _safe_div(
                player["kills"] + player["assists"], totals["kills"],
            ),
            "kda": _safe_div(player["kills"] + player["assists"], player["deaths"]),
            "damage_share": _safe_div(
                player["damage_to_champions"], totals["damage"],
            ),
            "damage_efficiency": _safe_div(
                player["damage_to_champions"], player["gold_earned"],
            ),
            "gold_per_minute": _safe_div(player["gold_earned"], minutes),
            "cs_per_minute": _safe_div(player["cs"], minutes),
            "objective_share": _safe_div(
                player["damage_to_objectives"], totals["objectives"],
            ),
            "turret_share": _safe_div(
                player["damage_to_turrets"], totals["turrets"],
            ),
            "vision_per_minute": _safe_div(player["vision_score"], minutes),
            "ward_actions_per_minute": _safe_div(
                player["wards_placed"] + player["wards_killed"], minutes,
            ),
            "deaths_per_minute": _safe_div(player["deaths"], minutes),
            "utility_per_minute": _safe_div(
                player["damage_mitigated"] + player["healing"], minutes,
            ),
        }
        raw_rows.append(row)
        for key, value in row.items():
            metrics[key].append(value)

    normalized = {
        key: _percentiles(values, lower_is_better=(key == "deaths_per_minute"))
        for key, values in metrics.items()
    }

    scored = []
    for index, player in enumerate(participants):
        component_values = {
            "combat": (
                normalized["kill_participation"][index]
                + normalized["kda"][index]
                + normalized["damage_share"][index]
                + normalized["damage_efficiency"][index]
            ) / 4,
            "economy": (
                normalized["gold_per_minute"][index]
                + normalized["cs_per_minute"][index]
            ) / 2,
            "objectives": (
                normalized["objective_share"][index]
                + normalized["turret_share"][index]
            ) / 2,
            "vision": (
                normalized["vision_per_minute"][index]
                + normalized["ward_actions_per_minute"][index]
            ) / 2,
            "teamplay": (
                normalized["deaths_per_minute"][index]
                + normalized["utility_per_minute"][index]
                + normalized["kill_participation"][index]
            ) / 3,
        }
        weights = COMPONENT_WEIGHTS[_role_bucket(player.get("role", ""))]
        weighted = sum(component_values[name] * weights[name] for name in weights)
        total = max(0.0, min(100.0, 5.0 + weighted * 0.90 + (4.0 if player["win"] else 0.0)))
        scored.append({
            "participant_id": player["participant_id"],
            "model_version": SCORING_MODEL_VERSION,
            "total_score": round(total, 1),
            "_raw_score": total,
            "_weighted": weighted,
            "_damage": player["damage_to_champions"],
            "_kill_participation": raw_rows[index]["kill_participation"],
            "components": {
                name: round(value, 1) for name, value in component_values.items()
            },
            "observations": _component_observations(
                player, component_values, participants,
            ),
        })

    scored.sort(
        key=lambda row: (
            -row["_raw_score"], -row["_weighted"], -row["_damage"],
            -row["_kill_participation"], row["participant_id"],
        )
    )
    for rank, row in enumerate(scored, 1):
        row["match_rank"] = rank
        for private_key in ("_raw_score", "_weighted", "_damage", "_kill_participation"):
            row.pop(private_key)
    return scored
