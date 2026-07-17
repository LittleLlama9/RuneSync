"""Outcome-blind synthetic review panel for DAEMON Score v2.

The panel is deliberately model-provider agnostic. RuneSync exports sanitized
match packets and an immutable rubric, independent judge agents write strict
JSONL verdicts, and this module converts only order-stable, high-agreement
comparisons into the existing append-only ``corpus.review`` label format.
"""

from __future__ import annotations

import datetime
import copy
import itertools
import json
import random
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from corpus._privacy import scan_for_forbidden
from corpus.manifest import canonical_json, sha256_hex
from corpus.review import (
    PairwiseItem,
    ReviewLabel,
    VALID_RATIONALE_TAGS,
    build_presentation,
    make_label,
)
from score_v2.leakage import OutcomeLeakageError, assert_no_outcome_leakage

PANEL_SCHEMA_VERSION = 1
RUBRIC_VERSION = "1.0.0"
PANEL_ROUNDS = ("a", "b")
PANEL_FEATURE_BLOCKS = (
    "raw",
    "fight_influence",
    "objective_participation",
    "structure_pressure",
    "enablement_suppression",
    "vision_influence",
    "death_tempo",
    "resource_conversion",
    "phase_breakdown",
    "live_state",
)
FORBIDDEN_JUDGE_KEYS = frozenset({
    "total_score",
    "match_rank",
    "rank_confidence",
    "participant_confidence",
    "companion_score",
    "daemon_score",
    "performance_score",
    "op_score",
    "opgg_score",
    "ugg_score",
    "score_model_version",
    "score_version",
    "placement",
    "mvp",
})
FORBIDDEN_PARTICIPANT_REFERENCE_KEYS = frozenset({
    "participant_id",
    "lane_opponent",
    "killer",
    "victim",
})

RUBRIC = {
    "version": RUBRIC_VERSION,
    "purpose": (
        "Rank individual influence in one League of Legends match from "
        "structured evidence without using the match result or existing scores."
    ),
    "axes": [
        {
            "id": "combat_impact",
            "rule": (
                "Credit contextual kills, assists, trades, and fight presence; "
                "debit untraded or rapidly repeated deaths. Do not rank by KDA."
            ),
        },
        {
            "id": "economy_efficiency",
            "rule": (
                "Credit resources only when leads are converted into fights, "
                "structures, objectives, pressure, or ally advantage."
            ),
        },
        {
            "id": "objective_control",
            "rule": (
                "Credit setup, contest, direct secure, fight involvement, trade, "
                "or pressure. Mere proximity or an assist flag is weak evidence."
            ),
        },
        {
            "id": "teamfight_presence",
            "rule": (
                "Judge timing and influence in meaningful encounters, including "
                "frontline, peel, engage, follow-up, and survival cost."
            ),
        },
        {
            "id": "utility_support",
            "rule": (
                "Credit ally enablement, enemy suppression, useful vision, peel, "
                "and low-resource impact. Protect support and tank playstyles."
            ),
        },
        {
            "id": "laning_phase",
            "rule": (
                "Judge opponent-relative leads, weak-side stability, and whether "
                "lane advantages or deficits affected later map state."
            ),
        },
        {
            "id": "survivability",
            "rule": (
                "Judge death timing and cost, not death count alone. Distinguish "
                "productive sacrifice from avoidable tempo loss."
            ),
        },
        {
            "id": "role_context",
            "rule": (
                "Compare different roles by influence relative to their available "
                "resources and responsibilities, not by identical stat thresholds."
            ),
        },
    ],
    "hard_rules": [
        "Never infer or discuss which team won.",
        "Never use an existing DAEMON, OP.GG, U.GG, or companion score.",
        "Never rank from KDA, gold, CS, damage, vision, or objectives alone.",
        "Do not penalize tanks, supports, weak-side players, or split pushers for "
        "not resembling a carry stat line.",
        "Use ties when evidence cannot distinguish two players.",
        "Abstain for the entire match when the evidence is too incomplete.",
        "Every participant assessment must cite concrete dotted evidence paths.",
    ],
    "sources": [
        {
            "title": "The SIDO Performance Model for League of Legends",
            "url": "https://arxiv.org/abs/2403.04873",
        },
        {
            "title": "PandaSkill: Player Performance and Skill Rating in Esports",
            "url": "https://arxiv.org/abs/2501.10049",
        },
        {
            "title": "Esports Analytics through Encounter Detection",
            "url": "https://www.researchgate.net/publication/295553343",
        },
        {
            "title": "Large Language Models are not Fair Evaluators",
            "url": "https://arxiv.org/abs/2305.17926",
        },
        {
            "title": "LLM-Rubric",
            "url": "https://aclanthology.org/2024.acl-long.745/",
        },
    ],
}
RUBRIC_HASH = sha256_hex(canonical_json(RUBRIC))

JUDGE_SYSTEM_PROMPT = """\
You are one independent judge in the RuneSync DAEMON Score v2 synthetic review
panel. Apply the supplied immutable rubric to each packet. You are comparing
individual influence, not predicting the match result.

Hard constraints:
- Do not infer or mention win/loss.
- Do not use KDA or any single statistic as the ranking.
- Respect role, champion, resources, evidence completeness, and event context.
- Return every subject exactly once in ranking_tiers, unless abstaining.
- A tier may contain multiple subjects when the evidence supports a tie.
- For every subject, cite 1-6 dotted paths that exist in that subject's
  features object and provide controlled rationale_tags only.
- Output JSONL only, one object per packet, with no markdown or commentary.

Output schema:
{"packet_id":"...", "ranking_tiers":[["subject"],["subject","subject"]],
 "assessments":[{"subject_id":"...", "confidence":0.0,
 "rationale_tags":["combat_impact"],
 "evidence_paths":["fight_influence.kill_events"],
 "brief_reason":"Concise evidence-grounded reason."}],
 "overall_confidence":0.0, "abstain_reason":""}

If evidence is insufficient for the whole match, output empty ranking_tiers and
assessments plus a non-empty abstain_reason.
"""


class PanelValidationError(ValueError):
    """Raised when a panel packet or judgment is unsafe or malformed."""


def _atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_jsonl(path: Path, rows: Iterable[Mapping]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    temporary.replace(path)


def _resolve_path(features: Mapping, dotted_path: str):
    value = features
    for part in dotted_path.split("."):
        if not part or not isinstance(value, Mapping) or part not in value:
            raise PanelValidationError(
                f"evidence path {dotted_path!r} does not exist"
            )
        value = value[part]
    return value


def _scan_for_score_leakage(obj, path="$") -> list[str]:
    problems = []
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
            if normalized in FORBIDDEN_JUDGE_KEYS:
                problems.append(f"{path}.{key}: forbidden score/rank field")
            problems.extend(_scan_for_score_leakage(value, f"{path}.{key}"))
    elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
        for index, value in enumerate(obj):
            problems.extend(_scan_for_score_leakage(value, f"{path}[{index}]"))
    return problems


def _scan_for_participant_reference_leakage(obj, path="$") -> list[str]:
    problems = []
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
            if normalized in FORBIDDEN_PARTICIPANT_REFERENCE_KEYS:
                problems.append(
                    f"{path}.{key}: forbidden participant-reference field"
                )
            problems.extend(
                _scan_for_participant_reference_leakage(value, f"{path}.{key}")
            )
    elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
        for index, value in enumerate(obj):
            problems.extend(
                _scan_for_participant_reference_leakage(value, f"{path}[{index}]")
            )
    return problems


def _public_participant(
        block: Mapping, subject_id: str, team: str,
        subject_by_participant_id: Mapping[int, str],
) -> dict:
    score_problems = _scan_for_score_leakage(block)
    if score_problems:
        raise PanelValidationError(
            "participant contains score/rank leakage: "
            + "; ".join(score_problems)
        )
    baseline = block.get("baseline") or {}
    features = {
        key: copy.deepcopy(block[key])
        for key in PANEL_FEATURE_BLOCKS if key in block
    }
    conversion = features.get("resource_conversion")
    if isinstance(conversion, dict) and "lane_opponent" in conversion:
        opponent_id = conversion.pop("lane_opponent")
        if opponent_id is not None:
            try:
                conversion["lane_opponent_subject_id"] = (
                    subject_by_participant_id[int(opponent_id)]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise PanelValidationError(
                    "resource_conversion.lane_opponent does not identify "
                    "another packet subject"
                ) from exc
    reference_problems = _scan_for_participant_reference_leakage(features)
    if reference_problems:
        raise PanelValidationError(
            "participant contains raw participant references: "
            + "; ".join(reference_problems)
        )
    participant = {
        "subject_id": subject_id,
        "team": team,
        "champion_name": baseline.get("champion") or "Unknown",
        "role": baseline.get("role") or "unknown",
        "features": features,
    }
    assert_no_outcome_leakage(participant, context="synthetic panel participant")
    problems = scan_for_forbidden(participant)
    if problems:
        raise PanelValidationError(
            "panel participant contains disallowed data: " + "; ".join(problems)
        )
    return participant


def _packet_id_for_body(packet_body: Mapping) -> str:
    return sha256_hex(canonical_json(packet_body))[:24]


def _participant_evidence_fingerprint(participant: Mapping) -> str:
    features = copy.deepcopy(participant.get("features"))
    if isinstance(features, dict):
        conversion = features.get("resource_conversion")
        if isinstance(conversion, dict) and "lane_opponent_subject_id" in conversion:
            conversion["lane_opponent_subject_id"] = "<packet-subject>"
    return sha256_hex(canonical_json({
        "champion_name": participant.get("champion_name"),
        "role": participant.get("role"),
        "features": features,
    }))


def _subject_binding_hash(base_ref: str, participant: Mapping) -> str:
    return sha256_hex(canonical_json({
        "base_ref": base_ref,
        "participant_evidence_fingerprint": (
            _participant_evidence_fingerprint(participant)
        ),
    }))


def build_match_packet(
        stored_feature_set: Mapping, game_id: int, round_id: str,
) -> tuple[dict, dict]:
    """Build one public judge packet plus its private de-blinding map."""
    if round_id not in PANEL_ROUNDS:
        raise PanelValidationError(f"unknown panel round {round_id!r}")
    features = stored_feature_set.get("features") or {}
    assert_no_outcome_leakage(features, context=f"game {game_id} feature set")
    participants = features.get("participants") or {}
    if len(participants) != 10:
        raise PanelValidationError(
            f"game {game_id} has {len(participants)} participants; expected 10"
        )
    team_ids = sorted({
        str(block.get("team_id")) for block in participants.values()
    })
    if len(team_ids) != 2 or "None" in team_ids:
        raise PanelValidationError(
            f"game {game_id} does not contain exactly two identifiable teams"
        )

    rng = random.Random(f"score-v2-panel:{RUBRIC_HASH}:{round_id}:{game_id}")
    public_team_labels = ["A", "B"]
    rng.shuffle(public_team_labels)
    team_map = dict(zip(team_ids, public_team_labels))

    public_participants = []
    subject_map = {}
    subject_by_participant_id = {}
    for participant_id, block in participants.items():
        subject_id = secrets.token_hex(6)
        while subject_id in subject_map:
            subject_id = secrets.token_hex(6)
        subject_map[subject_id] = f"{game_id}:{int(participant_id)}"
        subject_by_participant_id[int(participant_id)] = subject_id
    for participant_id, block in participants.items():
        subject_id = subject_by_participant_id[int(participant_id)]
        public_participants.append(
            _public_participant(
                block, subject_id, team_map[str(block.get("team_id"))],
                subject_by_participant_id,
            )
        )
    rng.shuffle(public_participants)

    packet_body = {
        "schema_version": PANEL_SCHEMA_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "rubric_hash": RUBRIC_HASH,
        "evidence_source": features.get("evidence_source"),
        "evidence_completeness": features.get("chosen_source_completeness"),
        "duration_seconds": features.get("duration_seconds"),
        "feature_abstain": bool(features.get("abstain", False)),
        "feature_abstain_reason": features.get("abstain_reason"),
        "participants": public_participants,
    }
    packet_id = _packet_id_for_body(packet_body)
    packet = {"packet_id": packet_id, **packet_body}
    private_map = {
        "packet_id": packet_id,
        "game_id": int(game_id),
        "round_id": round_id,
        "rubric_hash": RUBRIC_HASH,
        "input_hash": stored_feature_set.get("input_hash") or "",
        "subjects": subject_map,
        "subject_bindings": {
            participant["subject_id"]: _subject_binding_hash(
                subject_map[participant["subject_id"]], participant,
            )
            for participant in public_participants
        },
    }
    return packet, private_map


def validate_panel_bundle(
        packets_by_round: Mapping[str, Mapping[str, Mapping]],
        private_document: Mapping,
) -> Mapping[str, Mapping]:
    """Verify public packets and their private de-blinding map as one bundle."""
    if private_document.get("schema_version") != PANEL_SCHEMA_VERSION:
        raise PanelValidationError("private map schema_version is stale or invalid")
    if private_document.get("rubric_hash") != RUBRIC_HASH:
        raise PanelValidationError("private map rubric_hash is stale or invalid")
    private_maps = private_document.get("packets")
    if not isinstance(private_maps, Mapping):
        raise PanelValidationError("private map packets must be an object")

    packet_ids = {
        packet_id
        for packets in packets_by_round.values()
        for packet_id in packets
    }
    if set(private_maps) != packet_ids:
        raise PanelValidationError(
            "private map packet IDs do not exactly match the public bundle"
        )

    refs_by_game_and_round = {}
    for round_id in PANEL_ROUNDS:
        packets = packets_by_round.get(round_id)
        if packets is None:
            raise PanelValidationError(f"public bundle is missing round {round_id}")
        seen_games = set()
        for packet_id, packet in packets.items():
            private_map = private_maps[packet_id]
            if private_map.get("packet_id") != packet_id:
                raise PanelValidationError(
                    f"private map packet_id mismatch for {packet_id}"
                )
            if private_map.get("round_id") != round_id:
                raise PanelValidationError(
                    f"private map round mismatch for {packet_id}"
                )
            if private_map.get("rubric_hash") != RUBRIC_HASH:
                raise PanelValidationError(
                    f"private map rubric mismatch for {packet_id}"
                )
            try:
                game_id = int(private_map["game_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PanelValidationError(
                    f"private map game_id is invalid for {packet_id}"
                ) from exc
            if game_id in seen_games:
                raise PanelValidationError(
                    f"round {round_id} contains duplicate game {game_id}"
                )
            seen_games.add(game_id)

            subjects = private_map.get("subjects")
            if not isinstance(subjects, Mapping):
                raise PanelValidationError(
                    f"private map subjects are invalid for {packet_id}"
                )
            public_subjects = {
                participant["subject_id"]
                for participant in packet["participants"]
            }
            if set(subjects) != public_subjects:
                raise PanelValidationError(
                    f"private/public subject mismatch for {packet_id}"
                )
            base_refs = set(subjects.values())
            expected_refs = {
                f"{game_id}:{participant_id}" for participant_id in range(1, 11)
            }
            if base_refs != expected_refs:
                raise PanelValidationError(
                    f"private map participant refs are invalid for {packet_id}"
                )
            bindings = private_map.get("subject_bindings")
            if not isinstance(bindings, Mapping) or set(bindings) != public_subjects:
                raise PanelValidationError(
                    f"private map subject bindings are invalid for {packet_id}"
                )
            public_by_subject = {
                participant["subject_id"]: participant
                for participant in packet["participants"]
            }
            for subject_id, base_ref in subjects.items():
                expected_binding = _subject_binding_hash(
                    base_ref, public_by_subject[subject_id],
                )
                if bindings[subject_id] != expected_binding:
                    raise PanelValidationError(
                        f"private map subject binding mismatch for {packet_id}"
                    )
            refs_by_game_and_round[(game_id, round_id)] = {
                base_ref: _participant_evidence_fingerprint(
                    public_by_subject[subject_id]
                )
                for subject_id, base_ref in subjects.items()
            }

    games_by_round = {
        round_id: {
            int(private_maps[packet_id]["game_id"])
            for packet_id in packets_by_round[round_id]
        }
        for round_id in PANEL_ROUNDS
    }
    if games_by_round["a"] != games_by_round["b"]:
        raise PanelValidationError("panel rounds do not contain the same games")
    for game_id in games_by_round["a"]:
        if (
                refs_by_game_and_round[(game_id, "a")]
                != refs_by_game_and_round[(game_id, "b")]):
            raise PanelValidationError(
                f"panel rounds do not bind the same evidence to participants "
                f"for game {game_id}"
            )
    return private_maps


def export_panel_inputs(
        store, output_dir: Path, *, evidence_source: str = "lcu_timeline",
) -> dict:
    """Export every newest exact-tier feature set into two shuffled rounds."""
    output_dir = Path(output_dir)
    newest_by_game = {}
    for stored in store.list_feature_sets():
        if stored.get("evidence_source") != evidence_source:
            continue
        newest_by_game.setdefault(int(stored["game_id"]), stored)
    if not newest_by_game:
        raise PanelValidationError(
            f"no {evidence_source!r} feature sets are available"
        )

    round_rows = {round_id: [] for round_id in PANEL_ROUNDS}
    private_maps = {}
    for game_id in sorted(newest_by_game):
        stored = newest_by_game[game_id]
        for round_id in PANEL_ROUNDS:
            packet, private_map = build_match_packet(stored, game_id, round_id)
            round_rows[round_id].append(packet)
            private_maps[packet["packet_id"]] = private_map

    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _atomic_json(output_dir / "rubric.json", RUBRIC)
    (output_dir / "judge-prompt.txt").write_text(
        JUDGE_SYSTEM_PROMPT, encoding="utf-8",
    )
    for round_id, rows in round_rows.items():
        _atomic_jsonl(output_dir / f"round-{round_id}.jsonl", rows)
    _atomic_json(output_dir / "private-map.json", {
        "schema_version": PANEL_SCHEMA_VERSION,
        "rubric_hash": RUBRIC_HASH,
        "generated_at": generated_at,
        "packets": private_maps,
    })
    return {
        "games": len(newest_by_game),
        "packets": sum(len(rows) for rows in round_rows.values()),
        "rubric_hash": RUBRIC_HASH,
        "output_dir": str(output_dir),
    }


@dataclass(frozen=True)
class SubjectAssessment:
    subject_id: str
    confidence: float
    rationale_tags: tuple[str, ...]
    evidence_paths: tuple[str, ...]
    brief_reason: str


@dataclass(frozen=True)
class PanelJudgment:
    packet_id: str
    ranking_tiers: tuple[tuple[str, ...], ...]
    assessments: tuple[SubjectAssessment, ...]
    overall_confidence: float
    abstain_reason: str


def validate_judgment(row: Mapping, packet: Mapping) -> PanelJudgment:
    if row.get("packet_id") != packet.get("packet_id"):
        raise PanelValidationError("judgment packet_id does not match input")
    overall_confidence = float(row.get("overall_confidence"))
    if not 0.0 <= overall_confidence <= 1.0:
        raise PanelValidationError("overall_confidence must be within [0, 1]")
    abstain_reason = str(row.get("abstain_reason") or "").strip()
    tiers = tuple(
        tuple(str(subject_id) for subject_id in tier)
        for tier in (row.get("ranking_tiers") or ())
    )
    assessments = tuple(
        SubjectAssessment(
            subject_id=str(item["subject_id"]),
            confidence=float(item["confidence"]),
            rationale_tags=tuple(item.get("rationale_tags") or ()),
            evidence_paths=tuple(item.get("evidence_paths") or ()),
            brief_reason=str(item.get("brief_reason") or "").strip(),
        )
        for item in (row.get("assessments") or ())
    )
    if abstain_reason:
        if tiers or assessments:
            raise PanelValidationError(
                "an abstained judgment must not contain rankings or assessments"
            )
        return PanelJudgment(
            packet_id=row["packet_id"], ranking_tiers=(),
            assessments=(), overall_confidence=overall_confidence,
            abstain_reason=abstain_reason,
        )

    expected = {
        participant["subject_id"] for participant in packet["participants"]
    }
    ranked = [subject_id for tier in tiers for subject_id in tier]
    if len(ranked) != len(set(ranked)) or set(ranked) != expected:
        raise PanelValidationError(
            "ranking_tiers must contain every packet subject exactly once"
        )
    by_subject = {
        participant["subject_id"]: participant
        for participant in packet["participants"]
    }
    if {item.subject_id for item in assessments} != expected:
        raise PanelValidationError(
            "assessments must contain every packet subject exactly once"
        )
    if len(assessments) != len(expected):
        raise PanelValidationError(
            "assessments must not contain duplicate subjects"
        )
    for assessment in assessments:
        if not 0.0 <= assessment.confidence <= 1.0:
            raise PanelValidationError(
                f"{assessment.subject_id}: confidence must be within [0, 1]"
            )
        if not assessment.rationale_tags:
            raise PanelValidationError(
                f"{assessment.subject_id}: rationale_tags are required"
            )
        unknown_tags = (
            set(assessment.rationale_tags) - set(VALID_RATIONALE_TAGS)
        )
        if unknown_tags:
            raise PanelValidationError(
                f"{assessment.subject_id}: unknown rationale tags "
                f"{sorted(unknown_tags)}"
            )
        if not 1 <= len(assessment.evidence_paths) <= 6:
            raise PanelValidationError(
                f"{assessment.subject_id}: 1-6 evidence paths are required"
            )
        for path in assessment.evidence_paths:
            _resolve_path(by_subject[assessment.subject_id]["features"], path)
        if not assessment.brief_reason or len(assessment.brief_reason) > 500:
            raise PanelValidationError(
                f"{assessment.subject_id}: brief_reason must be 1-500 characters"
            )
        problems = scan_for_forbidden(assessment.brief_reason)
        if problems:
            raise PanelValidationError(
                f"{assessment.subject_id}: brief_reason contains disallowed data"
            )
    return PanelJudgment(
        packet_id=row["packet_id"], ranking_tiers=tiers,
        assessments=assessments, overall_confidence=overall_confidence,
        abstain_reason="",
    )


def load_judgments(
        path: Path, packets: Mapping[str, Mapping],
) -> list[PanelJudgment]:
    judgments = []
    seen = set()
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                packet_id = row.get("packet_id")
                if packet_id not in packets:
                    raise PanelValidationError(
                        f"unknown packet_id {packet_id!r}"
                    )
                judgment = validate_judgment(row, packets[packet_id])
            except (
                    json.JSONDecodeError, KeyError, TypeError, ValueError,
                    OutcomeLeakageError, PanelValidationError) as exc:
                raise PanelValidationError(
                    f"{path.name} line {line_number}: {exc}"
                ) from exc
            if judgment.packet_id in seen:
                raise PanelValidationError(
                    f"{path.name}: duplicate packet_id {judgment.packet_id}"
                )
            seen.add(judgment.packet_id)
            judgments.append(judgment)
    missing = sorted(set(packets) - seen)
    if missing:
        raise PanelValidationError(
            f"{path.name}: missing {len(missing)} packet judgments"
        )
    return judgments


def _pair_relations(
        judgment: PanelJudgment, private_map: Mapping,
) -> tuple[dict[tuple[str, str], tuple[str, int]], dict[str, SubjectAssessment]]:
    tiers_by_ref = {}
    assessments_by_ref = {}
    assessment_by_subject = {
        item.subject_id: item for item in judgment.assessments
    }
    for tier_index, tier in enumerate(judgment.ranking_tiers):
        for subject_id in tier:
            base_ref = private_map["subjects"][subject_id]
            tiers_by_ref[base_ref] = tier_index
            assessments_by_ref[base_ref] = assessment_by_subject[subject_id]
    relations = {}
    for left_ref, right_ref in itertools.combinations(
            sorted(tiers_by_ref), 2):
        left_tier = tiers_by_ref[left_ref]
        right_tier = tiers_by_ref[right_ref]
        relation = (
            "tie" if left_tier == right_tier
            else ("left" if left_tier < right_tier else "right")
        )
        relations[(left_ref, right_ref)] = (
            relation, abs(left_tier - right_tier),
        )
    return relations, assessments_by_ref


def aggregate_judgments(
        *, packets_by_round: Mapping[str, Mapping[str, Mapping]],
        private_maps: Mapping[str, Mapping],
        judgments: Mapping[tuple[str, str], Sequence[PanelJudgment]],
        generated_at: str,
        min_reviewers: int = 3,
        min_agreement: float = 1.0,
        min_confidence: float = 0.65,
        max_tier_gap: int = 3,
) -> tuple[list[ReviewLabel], dict, dict]:
    """Convert stable, high-agreement panel rankings into review labels."""
    if min_reviewers < 1:
        raise PanelValidationError("min_reviewers must be at least 1")
    if not 0.0 <= min_agreement <= 1.0:
        raise PanelValidationError("min_agreement must be within [0, 1]")
    if not 0.0 <= min_confidence <= 1.0:
        raise PanelValidationError("min_confidence must be within [0, 1]")
    if max_tier_gap < 0:
        raise PanelValidationError("max_tier_gap must be at least 0")
    reviewer_ids = sorted({key[0] for key in judgments})
    for reviewer_id in reviewer_ids:
        missing_rounds = [
            round_id for round_id in PANEL_ROUNDS
            if (reviewer_id, round_id) not in judgments
        ]
        if missing_rounds:
            raise PanelValidationError(
                f"reviewer {reviewer_id!r} is missing rounds {missing_rounds}"
            )
    reviewer_stable = {}
    reviewer_counts = {}
    for reviewer_id in reviewer_ids:
        rounds = {}
        for round_id in PANEL_ROUNDS:
            rows = judgments.get((reviewer_id, round_id), ())
            by_game = {}
            for row in rows:
                private_map = private_maps[row.packet_id]
                if row.abstain_reason:
                    continue
                relations, assessments = _pair_relations(row, private_map)
                by_game[int(private_map["game_id"])] = (
                    relations, assessments, row.overall_confidence,
                )
            rounds[round_id] = by_game

        stable = {}
        for game_id in sorted(set(rounds["a"]) & set(rounds["b"])):
            relations_a, assessments_a, overall_a = rounds["a"][game_id]
            relations_b, assessments_b, overall_b = rounds["b"][game_id]
            for pair, (relation_a, gap_a) in relations_a.items():
                relation_b, gap_b = relations_b.get(pair, (None, None))
                if relation_b != relation_a:
                    continue
                confidences = [overall_a, overall_b]
                tags = set()
                evidence_paths = set()
                for base_ref in pair:
                    for assessment in (
                            assessments_a[base_ref], assessments_b[base_ref]):
                        confidences.append(assessment.confidence)
                        tags.update(assessment.rationale_tags)
                        evidence_paths.update(assessment.evidence_paths)
                stable[pair] = {
                    "relation": relation_a,
                    "tier_gap": max(gap_a, gap_b),
                    "confidence": min(confidences),
                    "rationale_tags": tuple(sorted(tags)),
                    "evidence_paths": tuple(sorted(evidence_paths)),
                }
        reviewer_stable[reviewer_id] = stable
        reviewer_counts[reviewer_id] = len(stable)

    all_pairs = sorted(set().union(
        *(set(rows) for rows in reviewer_stable.values())
    ))
    labels = []
    token_maps = {}
    accepted_pairs = 0
    rejected_disagreement = 0
    rejected_confidence = 0
    for pair in all_pairs:
        available = [
            (reviewer_id, reviewer_stable[reviewer_id][pair])
            for reviewer_id in sorted(reviewer_stable)
            if pair in reviewer_stable[reviewer_id]
        ]
        if len(available) < min_reviewers:
            continue
        if max(row["tier_gap"] for _, row in available) > max_tier_gap:
            continue
        relation_counts = {}
        for _, row in available:
            relation_counts[row["relation"]] = (
                relation_counts.get(row["relation"], 0) + 1
            )
        relation, count = max(
            relation_counts.items(), key=lambda item: (item[1], item[0])
        )
        if count / len(available) < min_agreement:
            rejected_disagreement += 1
            continue
        supporting = [
            (reviewer_id, row) for reviewer_id, row in available
            if row["relation"] == relation
        ]
        if min(row["confidence"] for _, row in supporting) < min_confidence:
            rejected_confidence += 1
            continue

        left_ref, right_ref = pair
        presentation, token_map = build_presentation(
            PairwiseItem(left_ref, {}), PairwiseItem(right_ref, {}),
            seed=f"synthetic-panel:{RUBRIC_HASH}",
        )
        token_maps[presentation.pair_id] = token_map
        if relation == "tie":
            choice = "tie"
        elif relation == "left":
            choice = (
                "left" if token_map["left_ref"] == left_ref else "right"
            )
        else:
            choice = (
                "left" if token_map["left_ref"] == right_ref else "right"
            )
        rationale_tags = tuple(sorted({
            tag for _, row in supporting for tag in row["rationale_tags"]
        }))
        evidence_paths = tuple(sorted({
            path for _, row in supporting for path in row["evidence_paths"]
        }))
        notes = (
            "Synthetic panel consensus; order-stable across rounds; evidence "
            "paths: " + ", ".join(evidence_paths[:12])
        )
        labels.append(make_label(
            pair_id=presentation.pair_id,
            reviewer_id=f"synthetic:panel-{RUBRIC_VERSION}",
            choice=choice,
            confidence=min(row["confidence"] for _, row in supporting),
            rationale_tags=rationale_tags or ("role_context",),
            notes=notes,
            presentation_seed=f"synthetic-panel:{RUBRIC_HASH}",
            now=datetime.datetime.fromisoformat(generated_at),
        ))
        accepted_pairs += 1

    report = {
        "schema_version": PANEL_SCHEMA_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "rubric_hash": RUBRIC_HASH,
        "generated_at": generated_at,
        "reviewers": sorted(reviewer_stable),
        "reviewer_order_stable_pair_counts": reviewer_counts,
        "candidate_pairs": len(all_pairs),
        "accepted_consensus_pairs": accepted_pairs,
        "rejected_disagreement_pairs": rejected_disagreement,
        "rejected_low_confidence_pairs": rejected_confidence,
        "label_rows": len(labels),
        "thresholds": {
            "min_reviewers": min_reviewers,
            "min_agreement": min_agreement,
            "min_confidence": min_confidence,
            "max_tier_gap": max_tier_gap,
        },
    }
    return labels, token_maps, report


def load_packets(path: Path) -> dict[str, dict]:
    packets = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            packet_id = row.get("packet_id")
            if not packet_id or packet_id in packets:
                raise PanelValidationError(
                    f"{path.name} line {line_number}: invalid duplicate packet_id"
                )
            if row.get("schema_version") != PANEL_SCHEMA_VERSION:
                raise PanelValidationError(
                    f"{path.name} line {line_number}: stale or invalid schema_version"
                )
            if row.get("rubric_version") != RUBRIC_VERSION:
                raise PanelValidationError(
                    f"{path.name} line {line_number}: stale or invalid rubric_version"
                )
            if row.get("rubric_hash") != RUBRIC_HASH:
                raise PanelValidationError(
                    f"{path.name} line {line_number}: stale or invalid rubric_hash"
                )
            packet_body = {
                key: value for key, value in row.items() if key != "packet_id"
            }
            if _packet_id_for_body(packet_body) != packet_id:
                raise PanelValidationError(
                    f"{path.name} line {line_number}: packet body hash mismatch"
                )
            participants = row.get("participants")
            if not isinstance(participants, list) or len(participants) != 10:
                raise PanelValidationError(
                    f"{path.name} line {line_number}: expected 10 participants"
                )
            subject_ids = [participant.get("subject_id") for participant in participants]
            if None in subject_ids or len(subject_ids) != len(set(subject_ids)):
                raise PanelValidationError(
                    f"{path.name} line {line_number}: invalid subject IDs"
                )
            team_counts = {
                team: sum(1 for participant in participants if participant.get("team") == team)
                for team in ("A", "B")
            }
            if team_counts != {"A": 5, "B": 5}:
                raise PanelValidationError(
                    f"{path.name} line {line_number}: expected five subjects per team"
                )
            reference_problems = _scan_for_participant_reference_leakage(row)
            if reference_problems:
                raise PanelValidationError(
                    f"{path.name} line {line_number}: raw participant references: "
                    + "; ".join(reference_problems)
                )
            assert_no_outcome_leakage(row, context=f"packet {packet_id}")
            problems = scan_for_forbidden(row)
            if problems:
                raise PanelValidationError(
                    f"packet {packet_id} contains disallowed data: "
                    + "; ".join(problems)
                )
            packets[packet_id] = row
    return packets


def save_aggregated_outputs(
        *, labels: Sequence[ReviewLabel], token_maps: Mapping,
        report: Mapping, labels_path: Path, token_map_path: Path,
        report_path: Path,
) -> None:
    _atomic_jsonl(labels_path, (label.to_dict() for label in labels))
    _atomic_json(token_map_path, token_maps)
    _atomic_json(report_path, report)
