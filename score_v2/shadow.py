"""Offline Score v2 evidence backfill and immutable v1/v2 comparison.

Shadow reports never save score runs and never move an active score pointer.
They may persist canonical feature sets only when the caller explicitly enables
feature backfill.
"""

from __future__ import annotations

import datetime
import math
import sqlite3
from collections import Counter
from typing import Iterable, Mapping, Optional, Sequence

from performance_score import ScoreRouter, ScoreRoutingError
from score_features import (
    AGGREGATE,
    FEATURE_VERSION,
    SOURCE_PRIORITY,
    detect_capabilities,
    extract_game_features,
)


class ShadowReportError(ValueError):
    """Raised when a shadow run would produce misleading output."""


def _mean(values: Sequence[float]) -> Optional[float]:
    return round(sum(values) / len(values), 4) if values else None


def _pearson(left: Sequence[float], right: Sequence[float]) -> Optional[float]:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean) for x, y in zip(left, right)
    )
    left_scale = math.sqrt(sum((x - left_mean) ** 2 for x in left))
    right_scale = math.sqrt(sum((y - right_mean) ** 2 for y in right))
    if left_scale == 0.0 or right_scale == 0.0:
        return None
    return round(numerator / (left_scale * right_scale), 4)


def _available_sources(capabilities) -> list[str]:
    return [
        source for source in SOURCE_PRIORITY
        if (
            capabilities.aggregate if source == AGGREGATE
            else getattr(capabilities, source)
        )
    ]


def _history_game_ids(store, limit: int) -> list[int]:
    safe_limit = max(1, min(int(limit), 10000))
    game_ids = []
    offset = 0
    while len(game_ids) < safe_limit:
        page = store.list_history(offset, min(100, safe_limit - len(game_ids)))
        if not page:
            break
        game_ids.extend(int(row["game_id"]) for row in page)
        offset += len(page)
        if len(page) < min(100, safe_limit - len(game_ids) + len(page)):
            break
    return game_ids[:safe_limit]


def _newest_v1_run(store, game_id: int) -> Optional[dict]:
    return next(
        (
            run for run in store.list_score_runs(game_id)
            if int(run["model_version"]) == 1
        ),
        None,
    )


def _ensure_feature_set(
        store, game_id: int, source: str, backfill_features: bool,
) -> Optional[dict]:
    stored = store.get_feature_set(
        game_id, feature_version=FEATURE_VERSION, evidence_source=source,
    )
    if stored is not None or not backfill_features:
        return stored
    extract_game_features(
        store, game_id, FEATURE_VERSION, evidence_source=source,
    )
    return store.get_feature_set(
        game_id, feature_version=FEATURE_VERSION, evidence_source=source,
    )


def _participant_comparison(
        v1_report: Mapping, routed_scores: Sequence[Mapping],
) -> list[dict]:
    v2_by_id = {
        int(row["participant_id"]): row for row in routed_scores
    }
    rows = []
    for player in v1_report["participants"]:
        participant_id = int(player["participant_id"])
        v2 = v2_by_id.get(participant_id)
        if v2 is None:
            continue
        v1_score = float(player["total_score"])
        v2_score = float(v2["total_score"])
        v1_rank = int(player["match_rank"])
        v2_rank = int(v2["match_rank"])
        rows.append({
            "participant_id": participant_id,
            "champion_name": player["champion_name"],
            "role": player["role"],
            "v1_score": v1_score,
            "v1_rank": v1_rank,
            "v2_score": v2_score,
            "v2_rank": v2_rank,
            "score_delta": round(v2_score - v1_score, 4),
            "rank_delta": v2_rank - v1_rank,
            "score_low": v2.get("score_low"),
            "score_high": v2.get("score_high"),
            "participant_confidence": v2.get("participant_confidence"),
            "rank_confidence": v2.get("rank_confidence"),
            "abstain": bool(v2.get("abstain")),
            "abstain_reasons": list(v2.get("abstain_reasons") or ()),
        })
    return rows


def _evaluate_expectation(expectation: Mapping, participants: Sequence[Mapping]) -> dict:
    expectation_type = expectation.get("type")
    if expectation_type == "compound":
        checks = [
            _evaluate_expectation(row, participants)
            for row in expectation.get("sub_expectations") or ()
        ]
        return {
            "type": expectation_type,
            "passed": bool(checks) and all(row["passed"] for row in checks),
            "checks": checks,
        }
    if expectation_type == "insufficient_evidence":
        passed = bool(participants) and all(row["abstain"] for row in participants)
        return {
            "type": expectation_type,
            "passed": passed,
            "detail": (
                "Every participant abstained."
                if passed else "At least one participant received a normal verdict."
            ),
        }
    if expectation_type == "pairwise_minimum_gap":
        by_champion = {row["champion_name"]: row for row in participants}
        winner = by_champion.get(expectation.get("winner"))
        loser = by_champion.get(expectation.get("loser"))
        if winner is None or loser is None:
            return {
                "type": expectation_type,
                "passed": False,
                "detail": "Named comparison participant was not found.",
            }
        gap = float(winner["v2_score"]) - float(loser["v2_score"])
        minimum = float(expectation.get("min_gap") or 0.0)
        passed = (
            not winner["abstain"]
            and not loser["abstain"]
            and gap >= minimum
            and int(winner["v2_rank"]) < int(loser["v2_rank"])
        )
        return {
            "type": expectation_type,
            "winner": winner["champion_name"],
            "loser": loser["champion_name"],
            "score_gap": round(gap, 4),
            "minimum_gap": minimum,
            "passed": passed,
        }
    return {
        "type": expectation_type or "unknown",
        "passed": False,
        "detail": "Expectation type is not supported by the shadow evaluator.",
    }


def _adversarial_results(
        cases: Iterable[Mapping], games: Sequence[Mapping],
) -> list[dict]:
    by_game = {
        int(row["game_id"]): row for row in games
        if row.get("status") == "scored"
    }
    results = []
    for case in cases:
        game_id = case.get("game_id")
        if game_id is None:
            continue
        game = by_game.get(int(game_id))
        if game is None:
            results.append({
                "case_id": case.get("case_id"),
                "game_id": int(game_id),
                "status": "not_evaluated",
                "passed": None,
                "reason": "No shadow score was produced for this game.",
            })
            continue
        evaluation = _evaluate_expectation(
            case.get("expectation") or {}, game["participants"],
        )
        results.append({
            "case_id": case.get("case_id"),
            "game_id": int(game_id),
            "status": "evaluated",
            **evaluation,
        })
    return results


def build_shadow_report(
        store,
        artifacts: Optional[Mapping] = None,
        *,
        game_ids: Optional[Sequence[int]] = None,
        limit: int = 100,
        backfill_features: bool = False,
        allow_development_artifacts: bool = False,
        adversarial_cases: Iterable[Mapping] = (),
        generated_at: Optional[datetime.datetime] = None,
) -> dict:
    """Build a deterministic evidence inventory and optional v1/v2 report."""
    artifacts = dict(artifacts or {})
    insufficient = sorted(
        source for source, artifact in artifacts.items()
        if artifact.training_metadata.get("status") == "insufficient_data"
    )
    if insufficient:
        raise ShadowReportError(
            "Refusing neutral insufficient-data artifacts for shadow scoring: "
            + ", ".join(insufficient)
        )
    try:
        router = (
            ScoreRouter(
                artifacts,
                allow_development_artifacts=allow_development_artifacts,
            )
            if artifacts else None
        )
    except ScoreRoutingError as exc:
        raise ShadowReportError(str(exc)) from exc

    selected_game_ids = (
        list(dict.fromkeys(int(game_id) for game_id in game_ids))
        if game_ids is not None
        else _history_game_ids(store, limit)
    )
    source_counts = Counter()
    status_counts = Counter()
    games = []
    v1_scores = []
    v2_scores = []
    score_errors = []
    rank_exact = []
    rank_within_one = []
    local_score_errors = []
    local_rank_exact = []
    nonabstained = 0
    compared_participants = 0

    for game_id in selected_game_ids:
        match = store.get_match(game_id)
        if match is None:
            games.append({
                "game_id": game_id, "status": "error",
                "error": "Match disappeared before shadow analysis.",
            })
            status_counts["error"] += 1
            continue
        capabilities = detect_capabilities(store, game_id)
        available_sources = _available_sources(capabilities)
        source = (
            router.select_source(
                available_sources, capabilities.quality_dict(),
            )
            if router is not None
            else capabilities.best_source()
        )
        if source is None:
            games.append({
                "game_id": game_id,
                "status": "no_supported_evidence",
                "available_sources": available_sources,
            })
            status_counts["no_supported_evidence"] += 1
            continue
        try:
            stored = _ensure_feature_set(
                store, game_id, source, backfill_features,
            )
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            games.append({
                "game_id": game_id, "status": "error",
                "source": source, "error": str(exc),
            })
            status_counts["error"] += 1
            continue
        source_counts[source] += 1
        if stored is None:
            games.append({
                "game_id": game_id,
                "status": "missing_feature_set",
                "source": source,
                "available_sources": available_sources,
            })
            status_counts["missing_feature_set"] += 1
            continue
        features = stored["features"]
        inventory = {
            "game_id": game_id,
            "status": "evidence_ready_no_artifact",
            "source": source,
            "feature_version": stored["feature_version"],
            "input_hash": stored["input_hash"],
            "completeness": features.get("chosen_source_completeness"),
            "feature_abstain": bool(features.get("abstain")),
            "feature_abstain_reason": features.get("abstain_reason"),
            "available_sources": available_sources,
        }
        if router is None:
            games.append(inventory)
            status_counts["evidence_ready_no_artifact"] += 1
            continue

        v1_run = _newest_v1_run(store, game_id)
        if v1_run is None:
            inventory.update(
                status="missing_v1_run",
                error="No immutable Score v1 run is available for comparison.",
            )
            games.append(inventory)
            status_counts["missing_v1_run"] += 1
            continue
        v1_report = store.get_score_run_report(game_id, int(v1_run["id"]))
        if not v1_report or len(v1_report["participants"]) != 10:
            inventory.update(
                status="invalid_v1_run",
                error="Score v1 comparison run is incomplete.",
            )
            games.append(inventory)
            status_counts["invalid_v1_run"] += 1
            continue
        try:
            routed = router.score_feature_set(
                features, stored["evidence"],
                local_participant_id=int(match["local_participant_id"]),
            )
        except (ScoreRoutingError, TypeError, ValueError) as exc:
            inventory.update(status="error", error=str(exc))
            games.append(inventory)
            status_counts["error"] += 1
            continue
        participants = _participant_comparison(v1_report, routed.scores)
        if len(participants) != 10:
            inventory.update(
                status="error",
                error="Shadow scorer did not return all ten participants.",
            )
            games.append(inventory)
            status_counts["error"] += 1
            continue
        local_id = int(match["local_participant_id"])
        local = next(
            row for row in participants
            if int(row["participant_id"]) == local_id
        )
        for row in participants:
            compared_participants += 1
            if row["abstain"]:
                continue
            nonabstained += 1
            v1_scores.append(float(row["v1_score"]))
            v2_scores.append(float(row["v2_score"]))
            score_errors.append(abs(float(row["score_delta"])))
            rank_exact.append(int(row["v1_rank"]) == int(row["v2_rank"]))
            rank_within_one.append(
                abs(int(row["v1_rank"]) - int(row["v2_rank"])) <= 1
            )
        if not local["abstain"]:
            local_score_errors.append(abs(float(local["score_delta"])))
            local_rank_exact.append(
                int(local["v1_rank"]) == int(local["v2_rank"])
            )
        active_after = store.get_match(game_id)["active_score_run_id"]
        inventory.update({
            "status": "scored",
            "v1_run_id": int(v1_run["id"]),
            "v2_artifact_model_version": routed.artifact_model_version,
            "v2_artifact_hash": routed.model_artifact_hash,
            "v2_model_family": routed.model_family,
            "v2_calibration_version": routed.calibration_version,
            "active_score_run_unchanged": (
                active_after == match["active_score_run_id"]
            ),
            "local": local,
            "participants": participants,
        })
        games.append(inventory)
        status_counts["scored"] += 1

    adversarial = _adversarial_results(adversarial_cases, games)
    development_sources = sorted(
        source for source, artifact in artifacts.items()
        if not artifact.production_ready
    )
    release_reasons = [
        "A shadow report is observational and cannot authorize release.",
    ]
    if not artifacts:
        release_reasons.append("No Score v2 artifacts were supplied.")
    if development_sources:
        release_reasons.append(
            "Development-only artifacts: " + ", ".join(development_sources)
        )
    if any(row.get("passed") is False for row in adversarial):
        release_reasons.append("At least one verified adversarial case failed.")
    current = generated_at or datetime.datetime.now(datetime.timezone.utc)
    return {
        "schema_version": 1,
        "generated_at": current.astimezone(datetime.timezone.utc).isoformat(),
        "feature_version": FEATURE_VERSION,
        "mode": "shadow_comparison" if artifacts else "evidence_inventory",
        "safety": {
            "saved_score_runs": False,
            "changed_active_score_runs": False,
            "feature_backfill_enabled": bool(backfill_features),
            "release_eligible": False,
            "release_blockers": release_reasons,
        },
        "artifacts": {
            source: {
                "model_version": artifact.model_version,
                "content_hash": artifact.content_hash,
                "model_family": artifact.model_family,
                "production_ready": artifact.production_ready,
                "training_status": artifact.training_metadata.get("status"),
            }
            for source, artifact in sorted(artifacts.items())
        },
        "summary": {
            "games_requested": len(selected_game_ids),
            "status_counts": dict(sorted(status_counts.items())),
            "source_counts": dict(sorted(source_counts.items())),
            "participants_compared": compared_participants,
            "nonabstained_coverage": (
                round(nonabstained / compared_participants, 4)
                if compared_participants else None
            ),
            "score_mae_vs_v1": _mean(score_errors),
            "score_pearson_vs_v1": _pearson(v1_scores, v2_scores),
            "exact_rank_agreement": _mean([float(value) for value in rank_exact]),
            "within_one_rank_agreement": _mean(
                [float(value) for value in rank_within_one]
            ),
            "local_score_mae_vs_v1": _mean(local_score_errors),
            "local_exact_rank_agreement": _mean(
                [float(value) for value in local_rank_exact]
            ),
        },
        "adversarial_cases": adversarial,
        "games": games,
    }
