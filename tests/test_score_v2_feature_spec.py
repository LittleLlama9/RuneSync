"""Tests for score_v2/feature_spec.py -- the canonical feature contract.

Sections:
  1. Allowlist structural integrity (unique names, valid directions, no
     outcome-leakage anywhere in the spec's own paths/names).
  2. Deliberately excluded raw/objective-assist fields are documented and
     genuinely absent.
  3. Extraction: present/missing handling, transforms, bool coercion,
     non-numeric leaves treated as absent, unavailable-tier blocks.
  4. resolve_role / resolve_champion.
  5. Per-tier feature contracts (aggregate's honest fallback-only set).
"""

import pytest

from score_v2.feature_spec import (
    CAPABILITY_ALWAYS,
    DIRECTION_NEGATIVE,
    DIRECTION_POSITIVE,
    DIRECTION_UNCONSTRAINED,
    EXCLUDED_OBJECTIVE_ASSIST_FIELDS,
    EXCLUDED_RAW_FIELDS,
    FEATURE_ALLOWLIST,
    FEATURE_NAMES,
    FeatureSpec,
    TIER_FEATURE_CONTRACTS,
    VALID_CAPABILITIES,
    extract_feature_value,
    extract_feature_vector,
    extract_raw_value,
    feature_contract_for_tier,
    resolve_champion,
    resolve_role,
)
from score_v2.leakage import OutcomeLeakageError, scan_for_outcome_leakage


# ── 1. allowlist integrity ───────────────────────────────────────────────────

def test_feature_names_are_unique():
    assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))


def test_every_spec_has_a_valid_direction():
    for spec in FEATURE_ALLOWLIST:
        assert spec.direction in (
            DIRECTION_POSITIVE, DIRECTION_NEGATIVE, DIRECTION_UNCONSTRAINED,
        )


def test_every_spec_has_a_valid_capability():
    for spec in FEATURE_ALLOWLIST:
        assert spec.required_capability in VALID_CAPABILITIES


def test_no_spec_path_or_name_is_outcome_shaped():
    for spec in FEATURE_ALLOWLIST:
        problems = scan_for_outcome_leakage({
            "name": spec.name, "path": ".".join(spec.path),
        })
        assert not problems, (spec.name, problems)


def test_invalid_direction_rejected():
    with pytest.raises(ValueError):
        FeatureSpec(
            name="bad", path=("raw", "kills"), direction=99,
            transform="identity", required_capability=CAPABILITY_ALWAYS, group="raw",
        )


def test_invalid_transform_rejected():
    with pytest.raises(ValueError):
        FeatureSpec(
            name="bad", path=("raw", "kills"), direction=DIRECTION_POSITIVE,
            transform="unknown", required_capability=CAPABILITY_ALWAYS, group="raw",
        )


def test_empty_path_rejected():
    with pytest.raises(ValueError):
        FeatureSpec(
            name="bad", path=(), direction=DIRECTION_POSITIVE,
            transform="identity", required_capability=CAPABILITY_ALWAYS, group="raw",
        )


def test_spec_round_trips_through_dict():
    spec = FEATURE_ALLOWLIST[0]
    restored = FeatureSpec.from_dict(spec.to_dict())
    assert restored == spec


# ── 2. deliberately excluded fields ──────────────────────────────────────────

def test_excluded_raw_fields_are_not_in_the_allowlist():
    allowlisted_raw_paths = {
        spec.path for spec in FEATURE_ALLOWLIST if spec.path[0] == "raw"
    }
    for field_name in EXCLUDED_RAW_FIELDS:
        assert ("raw", field_name) not in allowlisted_raw_paths, (
            f"{field_name} must stay excluded -- see EXCLUDED_RAW_FIELDS "
            "rationale (v1 vision/turret-damage regression)"
        )


def test_vision_score_and_damage_fields_specifically_excluded():
    # Direct regression guard for the vault K'Sante/Seraphine/Vel'Koz case.
    for name in (
        "vision_score", "wards_placed", "wards_killed", "damage_to_turrets",
        "damage_to_objectives", "damage_to_champions",
    ):
        assert name in EXCLUDED_RAW_FIELDS


def test_raw_gold_and_cs_specifically_excluded():
    assert "gold_earned" in EXCLUDED_RAW_FIELDS
    assert "cs" in EXCLUDED_RAW_FIELDS
    allowlisted_names = set(FEATURE_NAMES)
    assert "raw_gold_earned" not in allowlisted_names
    assert "raw_cs" not in allowlisted_names


def test_monster_assist_fields_excluded_as_monotonic_influence():
    assert "epic_monster_assists" in EXCLUDED_OBJECTIVE_ASSIST_FIELDS
    assert "grub_assists" in EXCLUDED_OBJECTIVE_ASSIST_FIELDS
    allowlisted_objective_paths = {
        spec.path for spec in FEATURE_ALLOWLIST if spec.path[0] == "objective_participation"
    }
    assert ("objective_participation", "epic_monster_assists") not in allowlisted_objective_paths
    assert ("objective_participation", "grub_assists") not in allowlisted_objective_paths


def test_objective_fight_involvements_replaces_raw_assist_credit():
    spec = next(s for s in FEATURE_ALLOWLIST if s.name == "objective_fight_involvements")
    assert spec.path == ("objective_participation", "objective_fight_involvements")
    assert spec.direction == DIRECTION_POSITIVE


# ── 3. extraction ────────────────────────────────────────────────────────────

def _full_block(**overrides):
    block = {
        "raw": {
            "kills": 6, "deaths": 3, "assists": 5, "gold_earned": 11000, "cs": 190,
            "vision_score": 40, "wards_placed": 10, "wards_killed": 2,
            "damage_to_champions": 18000, "damage_to_objectives": 4000,
            "damage_to_turrets": 2000,
        },
        "fight_influence": {
            "kill_events": 6, "death_events": 3, "assist_events": 5,
            "first_blood": True, "untraded_deaths": 1, "event_kill_participation": 0.55,
        },
        "objective_participation": {
            "epic_monster_secures": 1, "epic_monster_assists": 0, "grub_secures": 1,
            "grub_assists": 0, "objective_fight_involvements": 2, "turret_kills": 2,
            "turret_assists": 1, "turret_plates": 2, "inhibitor_kills": 0,
        },
        "structure_pressure": {"structure_secures": 2},
        "enablement_suppression": {"ally_enablement_assists": 1, "suppression_weight": 2.0},
        "vision_influence": {"available": False, "reason": "no ward events"},
        "death_tempo": {"death_count": 3, "rapid_death_pairs": 0},
        "resource_conversion": {
            "available": True, "lane_opponent": 2, "lead_windows": 3,
            "converted_lead_windows": 2, "conversion_rate": 0.67,
        },
        "live_state": {"available": False, "reason": "no live snapshots"},
        "baseline": {"role": "mid", "champion": "TestChamp", "patch": "14.1"},
    }
    block.update(overrides)
    return block


def test_extract_feature_vector_all_present_for_full_block():
    vector = extract_feature_vector(_full_block())
    # vision_actionable_rate and live_dead_sample_rate are structurally
    # absent for this block (unavailable tiers) -- every other feature
    # should be present.
    missing = {name for name, value in vector.items() if not value.present}
    assert missing == {"vision_actionable_rate", "live_dead_sample_rate"}


def test_extract_feature_vector_rejects_leakage():
    bad_block = _full_block()
    bad_block["local_win"] = True
    with pytest.raises(OutcomeLeakageError):
        extract_feature_vector(bad_block)


def test_missing_parent_block_reports_absent_not_error():
    block = _full_block(fight_influence=None)
    vector = extract_feature_vector(block)
    assert vector["fight_kill_events"].present is False
    assert vector["fight_kill_events"].raw is None
    assert vector["fight_kill_events"].transformed is None


def test_bool_leaf_coerced_to_float():
    spec = next(s for s in FEATURE_ALLOWLIST if s.name == "fight_first_blood")
    value = extract_feature_value({"fight_influence": {"first_blood": True}}, spec)
    assert value.present is True
    assert value.raw == 1.0


def test_non_numeric_leaf_treated_as_absent():
    spec = next(s for s in FEATURE_ALLOWLIST if s.name == "raw_kills")
    value = extract_feature_value({"raw": {"kills": "not-a-number"}}, spec)
    assert value.present is False


def test_log1p_transform_is_monotonic_and_zero_at_zero():
    spec = next(s for s in FEATURE_ALLOWLIST if s.name == "raw_kills")
    assert spec.apply_transform(0.0) == 0.0
    assert spec.apply_transform(1.0) > spec.apply_transform(0.0)
    assert spec.apply_transform(10.0) > spec.apply_transform(1.0)


def test_clamp01_transform_clamps_out_of_range_values():
    spec = next(s for s in FEATURE_ALLOWLIST if s.name == "vision_actionable_rate")
    assert spec.apply_transform(-0.5) == 0.0
    assert spec.apply_transform(1.5) == 1.0
    assert spec.apply_transform(0.5) == 0.5


def test_extract_raw_value_handles_missing_intermediate_dict():
    spec = next(s for s in FEATURE_ALLOWLIST if s.name == "resource_conversion_rate")
    assert extract_raw_value({"resource_conversion": None}, spec) is None
    assert extract_raw_value({}, spec) is None


# ── 4. role/champion resolution ─────────────────────────────────────────────

def test_resolve_role_and_champion():
    block = _full_block()
    assert resolve_role(block) == "mid"
    assert resolve_champion(block) == "TestChamp"


def test_resolve_role_defaults_to_unknown():
    assert resolve_role({}) == "unknown"
    assert resolve_champion({}) is None


# ── 5. per-tier feature contracts ────────────────────────────────────────────

def test_all_four_tiers_have_a_contract():
    assert set(TIER_FEATURE_CONTRACTS) == {
        "match_v5", "lcu_timeline", "live_client", "aggregate",
    }


def test_aggregate_contract_is_only_the_always_available_raw_kda():
    contract = feature_contract_for_tier("aggregate")
    names = {spec.name for spec in contract}
    assert names == {"raw_kills", "raw_deaths", "raw_assists"}


def test_match_v5_contract_includes_everything():
    contract = feature_contract_for_tier("match_v5")
    names = {spec.name for spec in contract}
    assert "vision_actionable_rate" in names
    assert "resource_conversion_rate" in names
    assert "live_dead_sample_rate" not in names  # match_v5 has no live snapshots


def test_lcu_timeline_contract_excludes_vision_actionable_rate():
    contract = feature_contract_for_tier("lcu_timeline")
    names = {spec.name for spec in contract}
    assert "vision_actionable_rate" not in names  # no ward events, verified
    assert "resource_conversion_rate" in names  # has minute frames
    assert "fight_kill_events" in names  # has event evidence


def test_live_client_contract_excludes_vision_and_conversion():
    contract = feature_contract_for_tier("live_client")
    names = {spec.name for spec in contract}
    assert "vision_actionable_rate" not in names  # no ward events
    assert "resource_conversion_rate" not in names  # no minute frames
    assert "live_dead_sample_rate" in names  # has live snapshots
    assert "fight_kill_events" in names  # has event evidence


def test_feature_contract_for_tier_rejects_unknown_tier():
    with pytest.raises(ValueError):
        feature_contract_for_tier("not_a_real_tier")


def test_tier_contracts_are_subsets_of_the_full_allowlist():
    allowlist_names = set(FEATURE_NAMES)
    for specs in TIER_FEATURE_CONTRACTS.values():
        assert {spec.name for spec in specs} <= allowlist_names
