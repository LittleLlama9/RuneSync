"""Selective, evidence-backed coaching for DAEMON Score v2.

This module never invents intent, hidden map state, or a generic "weakest
component." It turns persisted causal features and signed timeline events into:

* concise factual observations;
* at most one controllable primary focus; and
* one measurable challenge evaluated over the next five comparable games.

Advice is withheld unless evidence, confidence, and recurrence gates all pass.

The catalogue of controllable negative patterns is **role-specific** and lives
in ``coaching_catalog.json`` next to this module. It was authored by a
three-model expert panel and reconciled into a consensus set. Each rule declares
which roles it applies to (with a per-role priority weight), a declarative
trigger condition over real feature fields, and an optional role-aware
``suppress_if`` compensator that excuses a bad-looking signal when a
role-appropriate justification is present (for example, deaths that were traded,
or leads left unconverted because the player was enabling allies instead).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence


MIN_COACHING_CONFIDENCE = 0.65
MIN_TIMELINE_COMPLETENESS = 0.7
MIN_PATTERN_OCCURRENCES = 2
CHALLENGE_TARGET_SUCCESSES = 3
CHALLENGE_WINDOW_GAMES = 5

VALID_ROLES = frozenset({"top", "jungle", "mid", "bot", "support"})
VALID_OPS = frozenset({">=", "<=", ">", "<", "==", "!="})

# Fields that never carry usable signal on the historical LCU timeline tier and
# therefore must not back a triggerable pattern (they would silently never fire).
FORBIDDEN_FIELD_PREFIXES = ("vision_influence.", "live_state.")
FORBIDDEN_FIELDS = frozenset({
    "objective_participation.turret_plates",
    "structure_pressure.turret_plates",
})

_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "coaching_catalog.json")


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


# --------------------------------------------------------------------------- #
# Declarative catalogue interpreter
# --------------------------------------------------------------------------- #

def _nested(block: Mapping, *path, default=None):
    node = block
    for key in path:
        if not isinstance(node, Mapping):
            return default
        node = node.get(key)
    return default if node is None else node


def _field(block: Mapping, dotted: str):
    node = block
    for key in dotted.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _compare(left, op: str, right) -> bool:
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    return False


def _eval_condition(block: Mapping, cond: Mapping) -> bool:
    """Evaluate one leaf condition. None/missing fields are always False."""
    if "available" in cond:
        return bool(_field(block, f"{cond['available']}.available"))

    op = cond.get("op")
    if op not in VALID_OPS:
        return False

    if "num" in cond and "den" in cond:
        num = _field(block, cond["num"])
        den = _field(block, cond["den"])
        if not _is_number(num) or not _is_number(den):
            return False
        den_min = cond.get("den_min", 1)
        if den == 0 or den < den_min:
            return False
        return _compare(num / den, op, cond["value"])

    value = cond.get("value")
    field_value = _field(block, cond.get("field", ""))
    if isinstance(value, bool):
        actual = bool(field_value) if field_value is not None else False
        return _compare(actual, op, value)
    if not _is_number(field_value):
        return False
    return _compare(field_value, op, value)


def _eval_group(block: Mapping, group: Optional[Mapping]) -> bool:
    """A group triggers when every ``all`` passes and at least one ``any`` passes."""
    if not group:
        return False
    all_conds = group.get("all") or []
    any_conds = group.get("any") or []
    if not all_conds and not any_conds:
        return False
    if all_conds and not all(_eval_condition(block, c) for c in all_conds):
        return False
    if any_conds and not any(_eval_condition(block, c) for c in any_conds):
        return False
    return True


def _role_of(block: Mapping) -> Optional[str]:
    role = _field(block, "baseline.role")
    return role if isinstance(role, str) else None


def _rule_config_for_role(rule: Mapping, role: Optional[str]):
    """Return (priority, trigger, suppress_if) for a role, or None if N/A."""
    roles = rule.get("roles") or {}
    if role not in roles:
        return None
    priority = roles[role]
    trigger = rule.get("trigger")
    suppress = rule.get("suppress_if")
    override = (rule.get("role_overrides") or {}).get(role)
    if isinstance(override, Mapping):
        if "priority" in override:
            priority = override["priority"]
        if "trigger" in override:
            trigger = override["trigger"]
        if "suppress_if" in override:
            suppress = override["suppress_if"]
    return priority, trigger, suppress


def _rule_triggers(rule: Mapping, block: Mapping) -> bool:
    config = _rule_config_for_role(rule, _role_of(block))
    if config is None:
        return False
    _priority, trigger, suppress = config
    if not _eval_group(block, trigger):
        return False
    if suppress and _eval_group(block, suppress):
        return False
    return True


_TEMPLATE_RE = re.compile(r"\{([a-zA-Z0-9_.]+)(?::(pct|int|\d?f))?\}")


def _render_template(block: Mapping, template: str) -> str:
    def _replace(match: "re.Match") -> str:
        value = _field(block, match.group(1))
        spec = match.group(2)
        if value is None:
            return "?"
        if spec == "pct":
            try:
                return f"{float(value) * 100:.0f}%"
            except (TypeError, ValueError):
                return str(value)
        if spec == "int":
            try:
                return str(int(value))
            except (TypeError, ValueError):
                return str(value)
        if spec and spec.endswith("f"):
            digits = spec[:-1] or "1"
            try:
                return f"{float(value):.{int(digits)}f}"
            except (TypeError, ValueError):
                return str(value)
        if isinstance(value, float) and not value.is_integer():
            return f"{value:.2f}"
        return str(value)

    return _TEMPLATE_RE.sub(_replace, template)


def _iter_condition_fields(group: Optional[Mapping]) -> Iterable[str]:
    if not isinstance(group, Mapping):
        return
    for key in ("all", "any"):
        for cond in group.get(key) or []:
            if not isinstance(cond, Mapping):
                continue
            if "available" in cond:
                yield f"{cond['available']}.available"
            for field_key in ("field", "num", "den"):
                if field_key in cond and isinstance(cond[field_key], str):
                    yield cond[field_key]


def _iter_rule_fields(rule: Mapping) -> Iterable[str]:
    yield from _iter_condition_fields(rule.get("trigger"))
    yield from _iter_condition_fields(rule.get("suppress_if"))
    for override in (rule.get("role_overrides") or {}).values():
        if isinstance(override, Mapping):
            yield from _iter_condition_fields(override.get("trigger"))
            yield from _iter_condition_fields(override.get("suppress_if"))
    for match in _TEMPLATE_RE.finditer(rule.get("evidence_template", "")):
        yield match.group(1)


def lint_catalog(catalog: Mapping) -> list[str]:
    """Return a list of structural / policy problems; empty means healthy."""
    problems: list[str] = []
    rules = catalog.get("rules")
    if not isinstance(rules, list) or not rules:
        return ["catalog has no rules"]
    seen_ids: set[str] = set()
    for index, rule in enumerate(rules):
        tag = rule.get("focus_id", f"#{index}")
        if not rule.get("focus_id"):
            problems.append(f"{tag}: missing focus_id")
        elif rule["focus_id"] in seen_ids:
            problems.append(f"{tag}: duplicate focus_id")
        else:
            seen_ids.add(rule["focus_id"])
        if not rule.get("title"):
            problems.append(f"{tag}: missing title")
        roles = rule.get("roles")
        if not isinstance(roles, Mapping) or not roles:
            problems.append(f"{tag}: missing roles map")
        else:
            for role, priority in roles.items():
                if role not in VALID_ROLES:
                    problems.append(f"{tag}: unknown role {role!r}")
                if not isinstance(priority, (int, float)) or isinstance(priority, bool):
                    problems.append(f"{tag}: non-numeric priority for {role}")
        if not _eval_group_is_shaped(rule.get("trigger")):
            problems.append(f"{tag}: trigger must have a non-empty all/any group")
        for text_key in ("target", "measurement", "anti_gaming_guardrail"):
            if not rule.get(text_key):
                problems.append(f"{tag}: missing {text_key}")
        for field in _iter_rule_fields(rule):
            if field.startswith(FORBIDDEN_FIELD_PREFIXES):
                problems.append(f"{tag}: references unavailable field {field!r}")
            if field in FORBIDDEN_FIELDS:
                problems.append(f"{tag}: references always-zero field {field!r}")
        for op in _iter_condition_ops(rule):
            if op not in VALID_OPS:
                problems.append(f"{tag}: invalid op {op!r}")
    return problems


def _eval_group_is_shaped(group) -> bool:
    if not isinstance(group, Mapping):
        return False
    return bool(group.get("all") or group.get("any"))


def _iter_condition_ops(rule: Mapping) -> Iterable[str]:
    groups = [rule.get("trigger"), rule.get("suppress_if")]
    for override in (rule.get("role_overrides") or {}).values():
        if isinstance(override, Mapping):
            groups.extend([override.get("trigger"), override.get("suppress_if")])
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        for key in ("all", "any"):
            for cond in group.get(key) or []:
                if isinstance(cond, Mapping) and "available" not in cond and "op" in cond:
                    yield cond["op"]


def load_catalog(path: str = _CATALOG_PATH) -> dict:
    with open(path, encoding="utf-8") as handle:
        catalog = json.load(handle)
    problems = lint_catalog(catalog)
    if problems:
        raise ValueError(
            "coaching catalogue failed validation: " + "; ".join(problems)
        )
    return catalog


def _load_shipped_catalog() -> dict:
    """Load the bundled catalogue, degrading to an empty (coaching-off) set if
    the data file is absent.

    A missing file is treated as "coaching disabled" so a packaging slip cannot
    crash application import; a malformed/invalid file still raises loudly (and
    is caught by the test suite before shipping).
    """
    try:
        return load_catalog()
    except FileNotFoundError:
        return {"rules": []}


CATALOG = _load_shipped_catalog()
FOCUS_RULES: tuple[Mapping, ...] = tuple(CATALOG.get("rules", ()))


# --------------------------------------------------------------------------- #
# Observations (unchanged: timestamped facts, never inferred intent)
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Coaching selection
# --------------------------------------------------------------------------- #

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

    role = _role_of(participant_features)
    patterns = []
    for rule in FOCUS_RULES:
        config = _rule_config_for_role(rule, role)
        if config is None:
            continue
        if not _rule_triggers(rule, participant_features):
            continue
        prior_occurrences = sum(
            1 for block in recent_comparable_features if _rule_triggers(rule, block)
        )
        occurrences = 1 + prior_occurrences
        patterns.append({
            "focus_id": rule["focus_id"],
            "title": rule["title"],
            "occurrences": occurrences,
            "games_considered": 1 + len(recent_comparable_features),
            "current_evidence": _render_template(
                participant_features, rule.get("evidence_template", ""),
            ),
            "priority": config[0],
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

    rules_by_id = {rule["focus_id"]: rule for rule in FOCUS_RULES}
    recurring.sort(
        key=lambda pattern: (
            -pattern["priority"],
            -pattern["occurrences"],
            pattern["focus_id"],
        )
    )
    chosen_pattern = recurring[0]
    chosen = rules_by_id[chosen_pattern["focus_id"]]
    challenge = {
        "focus_id": chosen["focus_id"],
        "target_successes": CHALLENGE_TARGET_SUCCESSES,
        "window_games": CHALLENGE_WINDOW_GAMES,
        "target": chosen["target"],
        "measurement": chosen["measurement"],
        "anti_gaming_guardrail": chosen["anti_gaming_guardrail"],
    }
    return CoachingResult(
        observations=observations,
        eligible=True,
        primary_focus=chosen["title"],
        challenges=(challenge,),
        recurring_patterns=tuple(recurring),
        withheld_reasons=(),
    )
