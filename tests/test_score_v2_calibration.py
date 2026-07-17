"""Tests for score_v2/training/calibration.py -- role/score calibration.

Sections:
  1. Role calibration shrinkage math (n=0 -> offset 0; larger n -> offset
     closer to raw mean; unknown roles bucket separately).
  2. Score calibration scale fitting and its small-sample fallback.
  3. Default confidence/abstention parameter shape.
"""

from score_v2.artifact import RoleCalibration
from score_v2.training.baseline import fit_pairwise_baseline
from score_v2.training.calibration import (
    DEFAULT_SCORE_SCALE,
    default_abstention_params,
    default_confidence_params,
    fit_role_calibration,
    fit_score_calibration,
    neutral_role_calibration,
    neutral_score_calibration,
    raw_linear_score,
)
from score_v2.training.dataset import TrainingDataset, build_feature_record


def _gf(kills, deaths, role, abstain=False):
    return {
        "duration_seconds": 1800.0, "abstain": abstain, "abstain_reason": None,
        "chosen_source_completeness": 1.0,
        "participants": {"1": {
            "raw": {"kills": kills, "deaths": deaths, "assists": 3},
            "baseline": {"role": role, "champion": "TestChamp", "patch": "14.1"},
        }},
    }


def _record(game_id, kills, deaths, role="mid", abstain=False):
    return build_feature_record(
        game_id=game_id, participant_id=1, evidence_source="match_v5",
        features_for_game=_gf(kills, deaths, role, abstain=abstain), split="train",
    )


def test_role_calibration_zero_sample_role_has_zero_offset_and_weight():
    records = (_record(1, 8, 1, role="mid"),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    fitted = fit_pairwise_baseline(dataset)  # zero pairs -> all-zero coefficients
    calibration = fit_role_calibration(dataset, fitted)
    assert calibration["support"].sample_count == 0
    assert calibration["support"].offset == 0.0
    assert calibration["support"].shrinkage_weight == 0.0


def test_role_calibration_shrinks_toward_zero_with_few_samples():
    # Build a dataset where "top" always has a much higher raw_kills value
    # than everyone else, using nonzero coefficients directly (bypass
    # fitting) to isolate the calibration math itself.
    records = tuple(_record(game_id, kills=9, deaths=0, role="top") for game_id in range(1, 4))
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    fitted = fit_pairwise_baseline(dataset)
    # Manually force a nonzero coefficient to get a nonzero raw_linear_score.
    fitted.coefficients["raw_kills"] = 2.0
    calibration = fit_role_calibration(dataset, fitted, shrinkage_k=5.0)
    top_calibration = calibration["top"]
    assert top_calibration.sample_count == 3
    raw_mean = sum(raw_linear_score(r, fitted) for r in records) / len(records)
    expected_weight = 3 / (3 + 5.0)
    assert abs(top_calibration.shrinkage_weight - expected_weight) < 1e-9
    assert abs(top_calibration.offset - raw_mean * expected_weight) < 1e-9
    # Larger n shrinks less (closer to 1.0) -- add more identical rows.
    more_records = records + tuple(
        _record(game_id, kills=9, deaths=0, role="top") for game_id in range(4, 24)
    )
    more_dataset = TrainingDataset(schema_version=1, feature_records=more_records, pair_labels=())
    more_calibration = fit_role_calibration(more_dataset, fitted, shrinkage_k=5.0)
    assert more_calibration["top"].shrinkage_weight > top_calibration.shrinkage_weight


def test_role_calibration_separates_unknown_role_bucket():
    records = (_record(1, 8, 1, role="not_a_real_role"),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    fitted = fit_pairwise_baseline(dataset)
    calibration = fit_role_calibration(dataset, fitted)
    assert calibration["unknown"].sample_count == 1


def test_score_calibration_fallback_with_fewer_than_two_records():
    records = (_record(1, 8, 1),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    fitted = fit_pairwise_baseline(dataset)
    role_calibration = fit_role_calibration(dataset, fitted)
    score_calibration = fit_score_calibration(dataset, fitted, role_calibration)
    assert score_calibration["scale"] == DEFAULT_SCORE_SCALE
    assert score_calibration["midpoint"] == 50.0
    assert score_calibration["clip_min"] == 0.0
    assert score_calibration["clip_max"] == 100.0


def test_score_calibration_uses_real_spread_when_available():
    records = tuple(_record(game_id, kills=game_id, deaths=0) for game_id in range(1, 6))
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    fitted = fit_pairwise_baseline(dataset)
    fitted.coefficients["raw_kills"] = 1.0  # force nonzero spread
    role_calibration = fit_role_calibration(dataset, fitted)
    score_calibration = fit_score_calibration(dataset, fitted, role_calibration)
    assert score_calibration["scale"] != DEFAULT_SCORE_SCALE
    assert score_calibration["scale"] > 0.0


def test_default_confidence_and_abstention_params_shape():
    confidence = default_confidence_params()
    for key in (
        "missing_feature_penalty", "evidence_quality_weight",
        "interval_min_half_width", "interval_max_half_width",
    ):
        assert key in confidence

    abstention = default_abstention_params()
    for key in (
        "short_game_seconds", "min_present_feature_fraction", "min_confidence_to_report",
    ):
        assert key in abstention


# ── abstained-record exclusion (default) and override ───────────────────────

def test_fit_role_calibration_excludes_abstained_records_by_default():
    normal = _record(1, kills=9, deaths=0, role="top")
    abstained = _record(2, kills=9, deaths=0, role="top", abstain=True)
    dataset = TrainingDataset(schema_version=1, feature_records=(normal, abstained), pair_labels=())
    fitted = fit_pairwise_baseline(dataset)
    calibration = fit_role_calibration(dataset, fitted)
    assert calibration["top"].sample_count == 1


def test_fit_role_calibration_include_abstained_overrides_exclusion():
    normal = _record(1, kills=9, deaths=0, role="top")
    abstained = _record(2, kills=9, deaths=0, role="top", abstain=True)
    dataset = TrainingDataset(schema_version=1, feature_records=(normal, abstained), pair_labels=())
    fitted = fit_pairwise_baseline(dataset)
    calibration = fit_role_calibration(dataset, fitted, include_abstained=True)
    assert calibration["top"].sample_count == 2


def test_fit_score_calibration_excludes_abstained_records_by_default():
    normal_records = tuple(_record(gid, kills=gid, deaths=0) for gid in range(1, 6))
    abstained = _record(99, kills=999, deaths=0, abstain=True)  # extreme outlier, must be ignored
    dataset = TrainingDataset(
        schema_version=1, feature_records=normal_records + (abstained,), pair_labels=(),
    )
    fitted = fit_pairwise_baseline(dataset)
    fitted.coefficients["raw_kills"] = 1.0
    role_calibration = fit_role_calibration(dataset, fitted)
    with_abstained_dataset = TrainingDataset(
        schema_version=1, feature_records=normal_records + (abstained,), pair_labels=(),
    )
    excluded_scale = fit_score_calibration(dataset, fitted, role_calibration)["scale"]
    included_scale = fit_score_calibration(
        with_abstained_dataset, fitted, role_calibration, include_abstained=True,
    )["scale"]
    assert excluded_scale != included_scale


# ── genuinely neutral calibration (insufficient-data path) ─────────────────

def test_neutral_role_calibration_is_all_zero_offset_and_weight():
    records = (_record(1, kills=9, deaths=0, role="top"), _record(2, kills=1, deaths=9, role="mid"))
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    calibration = neutral_role_calibration(dataset)
    for role, cal in calibration.items():
        assert cal.offset == 0.0
        assert cal.shrinkage_weight == 0.0
    assert calibration["top"].sample_count == 1
    assert calibration["mid"].sample_count == 1
    assert calibration["support"].sample_count == 0


def test_neutral_role_calibration_excludes_abstained_by_default():
    abstained = _record(1, kills=9, deaths=0, role="top", abstain=True)
    dataset = TrainingDataset(schema_version=1, feature_records=(abstained,), pair_labels=())
    calibration = neutral_role_calibration(dataset)
    assert calibration["top"].sample_count == 0


def test_neutral_score_calibration_uses_fixed_default_scale():
    calibration = neutral_score_calibration()
    assert calibration["scale"] == DEFAULT_SCORE_SCALE
    assert calibration["midpoint"] == 50.0
    assert calibration["clip_min"] == 0.0
    assert calibration["clip_max"] == 100.0
