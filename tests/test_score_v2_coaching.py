import json
import os

import pytest

from score_v2 import coaching
from score_v2.coaching import build_coaching, build_observations


FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "coaching_feature_paths.json"
)


def _block(role="top", **fields):
    """Build a participant feature block with baseline.role and nested overrides.

    `fields` are dotted paths, e.g. ``**{"fight_influence.untraded_deaths": 6}``.
    """
    block = {
        "baseline": {"role": role},
        "raw": {"kills": 4, "deaths": 5, "assists": 6},
        "fight_influence": {
            "untraded_deaths": 0, "traded_deaths": 0, "death_events": 0,
            "event_kill_participation": 0.5, "kill_events": 0, "assist_events": 0,
            "first_blood": False,
        },
        "death_tempo": {
            "death_count": 5, "rapid_death_pairs": 0,
            "deaths_by_phase": {"early": 0, "mid": 0, "late": 0},
        },
        "enablement_suppression": {
            "ally_enablement_assists": 0, "suppression_events": 10,
            "suppression_weight": 0.0,
        },
        "objective_participation": {
            "direct_objective_contacts": 0, "objective_fight_involvements": 0,
            "epic_monster_assists": 0, "epic_monster_secures": 0,
            "grub_assists": 0, "grub_secures": 0, "turret_kills": 0,
            "turret_assists": 0, "inhibitor_kills": 0,
        },
        "resource_conversion": {
            "available": True, "lead_windows": 0, "converted_lead_windows": 0,
            "conversion_rate": None,
        },
        "structure_pressure": {
            "structure_secures": 0, "structure_assists": 0,
            "isolated_frame_samples": 0,
        },
        "vision_influence": {"available": False},
        "live_state": {"available": False},
    }
    for dotted, value in fields.items():
        node = block
        keys = dotted.split(".")
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value
    return block


def _signed(participant_id, minute, metric, sign):
    return {
        "kind": "signed_event",
        "participant_id": participant_id,
        "t_ms": minute * 60_000,
        "phase": "mid",
        "source": "match_v5",
        "metric": metric,
        "sign": sign,
    }


# --------------------------------------------------------------------------- #
# Observations (unchanged behaviour)
# --------------------------------------------------------------------------- #

def test_observations_are_timestamped_facts_not_component_prose():
    observations = build_observations(
        _block(), 1,
        (
            _signed(1, 9, "objective_secure", 1),
            _signed(1, 12, "death", -1),
            _signed(2, 13, "champion_kill", 1),
        ),
        "match_v5",
    )
    assert observations == (
        "09:00 - Secured an epic objective.",
        "12:00 - Died; the timeline records the event without inferring intent.",
    )
    assert not any("component" in text.lower() for text in observations)


def test_aggregate_observation_discloses_missing_timeline_context():
    observations = build_observations(_block(), 1, (), "aggregate")
    assert observations[0] == "Post-game totals: 4/5/6."
    assert "no causal event observation is claimed" in observations[1]


# --------------------------------------------------------------------------- #
# Declarative interpreter primitives
# --------------------------------------------------------------------------- #

def test_missing_and_none_fields_never_trigger_or_crash():
    assert coaching._eval_condition({}, {"field": "a.b", "op": ">=", "value": 1}) is False
    block = _block()
    block["resource_conversion"]["conversion_rate"] = None
    assert coaching._eval_condition(
        block, {"field": "resource_conversion.conversion_rate", "op": "<", "value": 0.3}
    ) is False


def test_ratio_condition_guards_low_denominator():
    block = _block(**{
        "fight_influence.traded_deaths": 2, "death_tempo.death_count": 3,
    })
    cond = {
        "num": "fight_influence.traded_deaths",
        "den": "death_tempo.death_count",
        "op": ">=", "value": 0.5, "den_min": 5,
    }
    # denominator 3 < den_min 5 -> False even though 2/3 >= 0.5
    assert coaching._eval_condition(block, cond) is False
    cond2 = dict(cond, den_min=2)
    assert coaching._eval_condition(block, cond2) is True


def test_availability_condition_reads_available_flag():
    assert coaching._eval_condition(
        _block(), {"available": "resource_conversion"}
    ) is True
    assert coaching._eval_condition(
        _block(), {"available": "vision_influence"}
    ) is False


def test_boolean_condition_matches_flag():
    block = _block(**{"fight_influence.first_blood": True})
    assert coaching._eval_condition(
        block, {"field": "fight_influence.first_blood", "op": "==", "value": True}
    ) is True


def test_template_formats_pct_int_and_float():
    block = _block(**{
        "resource_conversion.conversion_rate": 0.333,
        "fight_influence.untraded_deaths": 6,
    })
    text = coaching._render_template(
        block,
        "{resource_conversion.conversion_rate:pct} over "
        "{fight_influence.untraded_deaths} deaths",
    )
    assert text == "33% over 6 deaths"


# --------------------------------------------------------------------------- #
# Role-specific behaviour
# --------------------------------------------------------------------------- #

def _eligible(block, prior, **kw):
    return build_coaching(
        block, 1, kw.get("evidence", ()), kw.get("source", "lcu_timeline"),
        confidence=kw.get("confidence", 0.9), completeness=kw.get("completeness", 1.0),
        abstain=kw.get("abstain", False), abstain_reasons=kw.get("abstain_reasons", ()),
        recent_comparable_features=prior,
    )


def test_untraded_death_pattern_recurs_into_a_challenge():
    block = _block(role="top", **{
        "fight_influence.untraded_deaths": 6, "death_tempo.death_count": 8,
    })
    result = _eligible(block, (block,))
    assert result.eligible is True
    assert result.primary_focus == "Make your deaths trade"
    assert result.challenges[0]["focus_id"] == "reduce_untraded_deaths"
    assert result.challenges[0]["target_successes"] == 3
    assert result.challenges[0]["window_games"] == 5


def test_single_game_issue_is_withheld_until_it_repeats():
    block = _block(role="top", **{
        "fight_influence.untraded_deaths": 6, "death_tempo.death_count": 8,
    })
    result = _eligible(block, ())
    assert result.eligible is False
    assert any("prior comparable game" in reason for reason in result.withheld_reasons)


def test_role_override_raises_threshold_for_mid():
    # top triggers at >=5 untraded, mid override requires >=6.
    top_block = _block(role="top", **{
        "fight_influence.untraded_deaths": 5, "death_tempo.death_count": 8,
    })
    mid_block = _block(role="mid", **{
        "fight_influence.untraded_deaths": 5, "death_tempo.death_count": 8,
    })
    assert _eligible(top_block, (top_block,)).eligible is True
    mid_result = _eligible(mid_block, (mid_block,))
    assert (
        mid_result.primary_focus != "Make your deaths trade"
        or mid_result.eligible is False
    )


def test_compensator_suppresses_untraded_deaths_when_mostly_traded():
    block = _block(role="top", **{
        "fight_influence.untraded_deaths": 6,
        "fight_influence.traded_deaths": 6,
        "death_tempo.death_count": 12,
    })
    result = _eligible(block, (block,))
    # deaths were half-traded, so the untraded-death rule is compensated away.
    if result.eligible:
        assert result.primary_focus != "Make your deaths trade"
    else:
        assert result.primary_focus is None


# --------------------------------------------------------------------------- #
# Gating
# --------------------------------------------------------------------------- #

def test_low_confidence_and_abstention_both_explain_withholding():
    block = _block(role="top", **{"fight_influence.untraded_deaths": 6})
    result = _eligible(
        block, (block,), confidence=0.4, abstain=True, abstain_reasons=("short_game",),
    )
    assert result.eligible is False
    assert any("Score abstained" in r for r in result.withheld_reasons)
    assert any("confidence" in r for r in result.withheld_reasons)


def test_aggregate_never_claims_causal_coaching_even_with_recurrence():
    block = _block(role="top", **{
        "fight_influence.untraded_deaths": 6, "death_tempo.death_count": 8,
    })
    result = _eligible(block, (block,), source="aggregate")
    assert result.eligible is False
    assert any("Aggregate evidence" in r for r in result.withheld_reasons)


def test_low_completeness_withholds():
    block = _block(role="top", **{"fight_influence.untraded_deaths": 6})
    result = _eligible(block, (block,), completeness=0.3)
    assert result.eligible is False
    assert any("completeness" in r for r in result.withheld_reasons)


# --------------------------------------------------------------------------- #
# Catalogue linter
# --------------------------------------------------------------------------- #

def test_lint_rejects_forbidden_and_malformed_rules():
    bad = {
        "rules": [
            {
                "focus_id": "vision_bad", "title": "x",
                "roles": {"top": 50},
                "trigger": {"all": [
                    {"field": "vision_influence.vision_actionable_rate", "op": "<", "value": 0.3}
                ]},
                "target": "t", "measurement": "m", "anti_gaming_guardrail": "g",
            },
            {
                "focus_id": "plate_bad", "title": "y",
                "roles": {"badrole": 50},
                "trigger": {"all": [
                    {"field": "objective_participation.turret_plates", "op": ">=", "value": 3}
                ]},
                "target": "t", "measurement": "m", "anti_gaming_guardrail": "g",
            },
            {
                "focus_id": "vision_bad", "title": "",
                "roles": {},
                "trigger": {},
                "target": "", "measurement": "", "anti_gaming_guardrail": "",
            },
        ]
    }
    problems = coaching.lint_catalog(bad)
    joined = " | ".join(problems)
    assert "unavailable field" in joined
    assert "always-zero field" in joined
    assert "unknown role" in joined
    assert "duplicate focus_id" in joined
    assert "non-empty all/any group" in joined


# --------------------------------------------------------------------------- #
# Shipped catalogue integrity (CI-safe: uses a checked-in fixture)
# --------------------------------------------------------------------------- #

def test_shipped_catalog_passes_lint():
    assert coaching.lint_catalog(coaching.CATALOG) == []


def test_shipped_catalog_covers_all_roles():
    covered = set()
    for rule in coaching.FOCUS_RULES:
        covered.update(rule.get("roles", {}))
    assert covered == coaching.VALID_ROLES


def test_shipped_catalog_only_references_real_fields():
    with open(FIXTURE, encoding="utf-8") as handle:
        valid_paths = set(json.load(handle)["field_paths"])
    for rule in coaching.FOCUS_RULES:
        for field in coaching._iter_rule_fields(rule):
            base = field[:-len(".available")] if field.endswith(".available") else field
            assert base in valid_paths or field in valid_paths, (
                f"{rule['focus_id']} references unknown field {field!r}"
            )


def test_conversion_rate_rules_require_lead_windows():
    """Every rule that reads conversion_rate must co-require lead_windows>=2."""
    for rule in coaching.FOCUS_RULES:
        groups = [rule.get("trigger")]
        for override in (rule.get("role_overrides") or {}).values():
            groups.append(override.get("trigger"))
        for group in groups:
            if not isinstance(group, dict):
                continue
            conds = (group.get("all") or []) + (group.get("any") or [])
            reads_rate = any(
                c.get("field") == "resource_conversion.conversion_rate" for c in conds
            )
            if not reads_rate:
                continue
            guards = [
                c for c in conds
                if c.get("field") == "resource_conversion.lead_windows"
                and c.get("op") in (">=", ">")
                and float(c.get("value", 0)) >= 2
            ]
            assert guards, f"{rule['focus_id']} uses conversion_rate without lead_windows>=2 guard"


def test_missing_catalog_file_raises():
    with pytest.raises(FileNotFoundError):
        coaching.load_catalog(os.path.join(os.path.dirname(__file__), "does_not_exist.json"))
