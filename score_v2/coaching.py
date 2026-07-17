"""Selective, evidence-backed coaching for DAEMON Score v2.

This module never invents intent, hidden map state, or a generic "weakest
component." It turns persisted causal features and signed timeline events into:

* concise factual observations;
* at most one controllable primary focus; and
* one measurable challenge evaluated over the next five comparable games.

Advice is withheld unless evidence, confidence, and recurrence gates all pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Optional, Sequence


MIN_COACHING_CONFIDENCE = 0.65
MIN_TIMELINE_COMPLETENESS = 0.7
MIN_PATTERN_OCCURRENCES = 2
CHALLENGE_TARGET_SUCCESSES = 3
CHALLENGE_WINDOW_GAMES = 5


@dataclass(frozen=True)
class CoachingResult:
    observations: tuple[str, ...]
    eligible: bool
    primary_focus: Optional[str]
    challenges: tuple[Mapping, ...]
    recurring_patterns: tuple[Mapping, ...]
    withheld_reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "eligible": self.eligible,
            "primary_focus": self.primary_focus,
            "challenges": [dict(challenge) for challenge in self.challenges],
            "recurring_patterns": [
                dict(pattern) for pattern in self.recurring_patterns
            ],
            "withheld_reasons": list(self.withheld_reasons),
        }


@dataclass(frozen=True)
class _FocusRule:
    focus_id: str
    title: str
    priority: int
    triggered: Callable[[Mapping], bool]
    evidence_summary: Callable[[Mapping], str]
    target: str
    measurement: str
    anti_gaming_guardrail: str


def _nested(block: Mapping, *path, default=None):
    node = block
    for key in path:
        if not isinstance(node, Mapping):
            return default
        node = node.get(key)
    return default if node is None else node


def _untraded_deaths(block: Mapping) -> int:
    return int(_nested(block, "fight_influence", "untraded_deaths", default=0) or 0)


def _rapid_death_pairs(block: Mapping) -> int:
    return int(_nested(block, "death_tempo", "rapid_death_pairs", default=0) or 0)


def _lead_windows(block: Mapping) -> int:
    return int(_nested(block, "resource_conversion", "lead_windows", default=0) or 0)


def _conversion_rate(block: Mapping) -> float:
    return float(_nested(block, "resource_conversion", "conversion_rate", default=0.0) or 0.0)


def _objective_contacts(block: Mapping) -> int:
    objective = _nested(block, "objective_participation", default={}) or {}
    return sum(
        int(objective.get(key) or 0)
        for key in ("epic_monster_assists", "grub_assists")
    )


def _objective_fight_involvements(block: Mapping) -> int:
    return int(
        _nested(
            block, "objective_participation",
            "objective_fight_involvements", default=0,
        ) or 0
    )


def _objective_secures(block: Mapping) -> int:
    objective = _nested(block, "objective_participation", default={}) or {}
    return sum(
        int(objective.get(key) or 0)
        for key in ("epic_monster_secures", "grub_secures")
    )


def _actionable_vision_opportunities(block: Mapping) -> int:
    vision = _nested(block, "vision_influence", default={}) or {}
    return int(vision.get("wards_placed_events") or 0) + int(
        vision.get("wards_killed_events") or 0
    )


def _actionable_vision_rate(block: Mapping) -> float:
    return float(
        _nested(
            block, "vision_influence", "vision_actionable_rate", default=0.0,
        ) or 0.0
    )


FOCUS_RULES: tuple[_FocusRule, ...] = (
    _FocusRule(
        focus_id="death_value",
        title="Reduce untraded deaths",
        priority=100,
        triggered=lambda block: _untraded_deaths(block) >= 2,
        evidence_summary=lambda block: (
            f"{_untraded_deaths(block)} deaths had no confirmed allied return kill "
            "inside the trade window."
        ),
        target="Finish with at most one untraded death in 3 of the next 5 comparable games.",
        measurement="Timeline-confirmed untraded_deaths <= 1.",
        anti_gaming_guardrail=(
            "Do not avoid necessary frontline or contest play solely to preserve "
            "the target; only timeline-confirmed trade context counts."
        ),
    ),
    _FocusRule(
        focus_id="reset_timing",
        title="Stabilize reset timing after deaths",
        priority=90,
        triggered=lambda block: _rapid_death_pairs(block) >= 1,
        evidence_summary=lambda block: (
            f"{_rapid_death_pairs(block)} repeated-death sequence(s) occurred "
            "inside the configured short interval."
        ),
        target="Record zero rapid-death pairs in 3 of the next 5 comparable games.",
        measurement="death_tempo.rapid_death_pairs == 0.",
        anti_gaming_guardrail=(
            "The target does not reward passive play; necessary fights remain "
            "valid when the reset, route, and objective timing are sound."
        ),
    ),
    _FocusRule(
        focus_id="lead_conversion",
        title="Convert lane leads into influence",
        priority=80,
        triggered=lambda block: (
            _lead_windows(block) >= 2 and _conversion_rate(block) < 0.5
        ),
        evidence_summary=lambda block: (
            f"Only {_conversion_rate(block) * 100:.0f}% of "
            f"{_lead_windows(block)} observable lead windows converted into a "
            "fight, objective, or structure event."
        ),
        target="Convert at least half of observable lead windows in 3 of the next 5 comparable games.",
        measurement="resource_conversion.conversion_rate >= 0.50 with at least 2 lead windows.",
        anti_gaming_guardrail=(
            "Do not force low-value fights to create credit; only conversions "
            "already recognized by the causal event window count."
        ),
    ),
    _FocusRule(
        focus_id="objective_influence",
        title="Make objective rotations influence the contest",
        priority=70,
        triggered=lambda block: (
            _objective_contacts(block) >= 2
            and _objective_fight_involvements(block) == 0
            and _objective_secures(block) == 0
        ),
        evidence_summary=lambda block: (
            f"{_objective_contacts(block)} objective assist contact(s) produced "
            "no confirmed nearby fight involvement."
        ),
        target="When recording 2+ objective contacts, add confirmed fight involvement or a direct secure in 3 of the next 5 eligible games.",
        measurement=(
            "objective_fight_involvements >= 1 or a direct epic/grub secure; "
            "games with fewer than 2 contacts are excluded."
        ),
        anti_gaming_guardrail=(
            "Do not abandon lane or force a contest only to satisfy the target; "
            "no real objective opportunity means the game is not eligible."
        ),
    ),
    _FocusRule(
        focus_id="vision_conversion",
        title="Place vision that converts into action",
        priority=60,
        triggered=lambda block: (
            _actionable_vision_opportunities(block) >= 3
            and _actionable_vision_rate(block) < 0.34
        ),
        evidence_summary=lambda block: (
            f"{_actionable_vision_rate(block) * 100:.0f}% of "
            f"{_actionable_vision_opportunities(block)} observable vision "
            "opportunities had a nearby allied follow-up."
        ),
        target="Reach at least a 34% actionable-vision rate in 3 of the next 5 comparable games.",
        measurement=(
            "vision_influence.actionable_vision_rate >= 0.34 with at least "
            "3 observable opportunities."
        ),
        anti_gaming_guardrail=(
            "Do not place unsafe or redundant wards to increase volume; only "
            "spatially and temporally linked allied follow-up counts."
        ),
    ),
)


_EVENT_TEXT = {
    "champion_kill": "Secured a champion kill.",
    "champion_kill_assist": "Contributed to a champion kill.",
    "death": "Died; the timeline records the event without inferring intent.",
    "objective_secure": "Secured an epic objective.",
    "objective_assist": "Received objective assist credit.",
    "structure_secure": "Secured a structure event.",
    "structure_assist": "Contributed to a structure event.",
}


def _format_time(t_ms) -> str:
    total_seconds = max(0, int(float(t_ms or 0) / 1000))
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


def build_observations(
        participant_features: Mapping,
        participant_id: int,
        evidence: Iterable[Mapping],
        evidence_source: str) -> tuple[str, ...]:
    """Return concise factual observations, preferring timestamped events."""
    signed = [
        row for row in evidence
        if row.get("kind") == "signed_event"
        and str(row.get("participant_id")) == str(participant_id)
        and row.get("metric") in _EVENT_TEXT
    ]
    negatives = sorted(
        (row for row in signed if int(row.get("sign", 0)) < 0),
        key=lambda row: float(row.get("t_ms") or 0),
    )
    positives = sorted(
        (row for row in signed if int(row.get("sign", 0)) > 0),
        key=lambda row: (
            {
                "objective_secure": 0,
                "structure_secure": 1,
                "champion_kill": 2,
                "objective_assist": 3,
                "structure_assist": 4,
                "champion_kill_assist": 5,
            }.get(row.get("metric"), 9),
            float(row.get("t_ms") or 0),
        ),
    )
    selected = positives[:2] + negatives[:2]
    selected.sort(key=lambda row: float(row.get("t_ms") or 0))
    observations = [
        f"{_format_time(row.get('t_ms'))} - {_EVENT_TEXT[row['metric']]}"
        for row in selected
    ]
    if observations:
        return tuple(observations)

    raw = participant_features.get("raw") or {}
    kills = int(raw.get("kills") or 0)
    deaths = int(raw.get("deaths") or 0)
    assists = int(raw.get("assists") or 0)
    return (
        f"Post-game totals: {kills}/{deaths}/{assists}.",
        (
            f"{evidence_source} supplied no participant-level timestamped "
            "events, so no causal event observation is claimed."
        ),
    )


def build_coaching(
        participant_features: Mapping,
        participant_id: int,
        evidence: Sequence[Mapping],
        evidence_source: str,
        confidence: float,
        completeness: float,
        abstain: bool,
        abstain_reasons: Sequence[str],
        recent_comparable_features: Sequence[Mapping] = (),
) -> CoachingResult:
    observations = build_observations(
        participant_features, participant_id, evidence, evidence_source,
    )
    withheld = []
    if abstain:
        withheld.append(
            "Score abstained: " + ", ".join(abstain_reasons or ("insufficient evidence",))
        )
    if confidence < MIN_COACHING_CONFIDENCE:
        withheld.append(
            f"Participant confidence {confidence:.2f} is below "
            f"{MIN_COACHING_CONFIDENCE:.2f}."
        )
    if evidence_source == "aggregate":
        withheld.append(
            "Aggregate evidence cannot verify fight, trade, or conversion context."
        )
    if completeness < MIN_TIMELINE_COMPLETENESS:
        withheld.append(
            f"Evidence completeness {completeness:.2f} is below "
            f"{MIN_TIMELINE_COMPLETENESS:.2f}."
        )

    patterns = []
    for rule in FOCUS_RULES:
        if not rule.triggered(participant_features):
            continue
        prior_occurrences = sum(
            1 for block in recent_comparable_features if rule.triggered(block)
        )
        occurrences = 1 + prior_occurrences
        patterns.append({
            "focus_id": rule.focus_id,
            "title": rule.title,
            "occurrences": occurrences,
            "games_considered": 1 + len(recent_comparable_features),
            "current_evidence": rule.evidence_summary(participant_features),
        })

    recurring = [
        pattern for pattern in patterns
        if pattern["occurrences"] >= MIN_PATTERN_OCCURRENCES
    ]
    if not recurring:
        if not recent_comparable_features:
            withheld.append(
                "Need at least one prior comparable game before calling a "
                "single-match issue a recurring pattern."
            )
        else:
            withheld.append(
                "No controllable negative pattern repeated across at least "
                f"{MIN_PATTERN_OCCURRENCES} comparable games."
            )

    if withheld or not recurring:
        return CoachingResult(
            observations=observations,
            eligible=False,
            primary_focus=None,
            challenges=(),
            recurring_patterns=tuple(patterns),
            withheld_reasons=tuple(withheld),
        )

    rules_by_id = {rule.focus_id: rule for rule in FOCUS_RULES}
    recurring.sort(
        key=lambda pattern: (
            -rules_by_id[pattern["focus_id"]].priority,
            -pattern["occurrences"],
            pattern["focus_id"],
        )
    )
    chosen_pattern = recurring[0]
    chosen = rules_by_id[chosen_pattern["focus_id"]]
    challenge = {
        "focus_id": chosen.focus_id,
        "target_successes": CHALLENGE_TARGET_SUCCESSES,
        "window_games": CHALLENGE_WINDOW_GAMES,
        "target": chosen.target,
        "measurement": chosen.measurement,
        "anti_gaming_guardrail": chosen.anti_gaming_guardrail,
    }
    return CoachingResult(
        observations=observations,
        eligible=True,
        primary_focus=chosen.title,
        challenges=(challenge,),
        recurring_patterns=tuple(recurring),
        withheld_reasons=(),
    )
