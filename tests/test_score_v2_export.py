"""Tests for score_v2/training/export.py -- artifact training/export.

Sections:
  1. dataset_for_tier correctly restricts records+pairs to one tier
     (base-ref pair resolution, not item_ref).
  2. Insufficient data -> honest "insufficient_data" status, GENUINELY
     neutral coefficients/normalization/role/score calibration (not just
     a masked/relabeled real fit), production_ready False, documented
     release_notes. Real ("exploratory") fits are only ever exported when
     the threshold is explicitly lowered below the usable-pair count.
  3. Sufficient data -> "fitted" status with a real (nonzero) signal.
  4. train_all_tiers only trains tiers actually present in the dataset.
  5. Each tier trains on its own canonical feature contract (aggregate:
     only the 3 always-available raw KDA features).
"""

from score_v2.feature_spec import feature_contract_for_tier
from score_v2.training.dataset import PairLabel, TrainingDataset, build_feature_record
from score_v2.training.export import dataset_for_tier, train_all_tiers, train_tier


def _gf(kills, deaths, tier, abstain=False):
    return {
        "duration_seconds": 1800.0, "abstain": abstain, "abstain_reason": None,
        "chosen_source_completeness": 1.0,
        "participants": {"1": {
            "raw": {"kills": kills, "deaths": deaths, "assists": 3},
            "baseline": {"role": "mid", "champion": "TestChamp", "patch": "14.1"},
        }},
    }


def _record(game_id, kills, deaths, tier="match_v5", abstain=False):
    return build_feature_record(
        game_id=game_id, participant_id=1, evidence_source=tier,
        features_for_game=_gf(kills, deaths, tier, abstain=abstain), split="train",
    )


def _pair(left_ref, right_ref, choice="left"):
    if choice == "left":
        winner_ref, relation = left_ref, "left_preferred"
    elif choice == "right":
        winner_ref, relation = right_ref, "right_preferred"
    else:
        winner_ref, relation = None, choice
    return PairLabel(
        pair_id=f"{left_ref}|{right_ref}", left_ref=left_ref, right_ref=right_ref,
        winner_ref=winner_ref, relation=relation, choice=choice, confidence=0.9,
        rationale_tags=("combat_impact",), reviewer_id="r1",
        created_at="2026-01-01T00:00:00+00:00",
    )


# ── 1. dataset_for_tier ──────────────────────────────────────────────────────

def test_dataset_for_tier_restricts_records_and_pairs():
    r1 = _record(1, 8, 1, tier="match_v5")
    r2 = _record(2, 2, 8, tier="aggregate")
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=())
    match_v5_only = dataset_for_tier(dataset, "match_v5")
    assert [r.item_ref for r in match_v5_only.feature_records] == ["1:1:match_v5"]
    aggregate_only = dataset_for_tier(dataset, "aggregate")
    assert [r.item_ref for r in aggregate_only.feature_records] == ["2:1:aggregate"]


def test_dataset_for_tier_resolves_pairs_via_base_ref_per_tier():
    # The same base_ref pair applies to match_v5 records when they exist,
    # and is correctly excluded for a tier lacking those participants.
    match_v5_a = _record(1, 8, 1, tier="match_v5")
    match_v5_b = _record(2, 1, 8, tier="match_v5")
    aggregate_a = _record(1, 8, 1, tier="aggregate")  # same base_ref as match_v5_a
    pair = _pair("1:1", "2:1", "left")
    dataset = TrainingDataset(
        schema_version=1, feature_records=(match_v5_a, match_v5_b, aggregate_a),
        pair_labels=(pair,),
    )
    match_v5_only = dataset_for_tier(dataset, "match_v5")
    assert len(match_v5_only.pair_labels) == 1  # both sides present in match_v5
    aggregate_only = dataset_for_tier(dataset, "aggregate")
    assert len(aggregate_only.pair_labels) == 0  # "2:1" has no aggregate record


# ── 2. insufficient data ─────────────────────────────────────────────────────

def test_train_tier_insufficient_data_is_honest():
    records = (_record(1, 8, 1),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    result = train_tier(
        dataset, "match_v5", model_version="0.0.1-dev", feature_version="2.0.0-evidence",
        calibration_version="0.0.1-dev", min_pairs_for_nontrivial_fit=20,
    )
    assert result.status == "insufficient_data"
    assert result.n_pairs_used == 0
    assert result.artifact.production_ready is False
    assert "NOT production-trained" in result.artifact.release_notes
    assert all(c.coefficient == 0.0 for c in result.artifact.coefficients)


def test_train_tier_insufficient_data_discards_real_fit_entirely():
    """Below-threshold usable pairs must produce a GENUINELY neutral
    artifact -- not merely an "insufficient_data" label slapped on
    whatever the underlying (statistically unreliable) fit produced.
    """
    records = tuple(
        _record(game_id, kills=k, deaths=d)
        for game_id, (k, d) in enumerate([(9, 0), (0, 9), (8, 1), (1, 8)], start=1)
    )
    # A real, learnable signal -- but only 2 pairs, well under any sane
    # min_pairs_for_nontrivial_fit default.
    pairs = (_pair("1:1", "2:1", "left"), _pair("3:1", "4:1", "left"))
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    result = train_tier(
        dataset, "match_v5", model_version="0.0.1-dev", feature_version="2.0.0-evidence",
        calibration_version="0.0.1-dev", min_pairs_for_nontrivial_fit=20,
    )
    assert result.status == "insufficient_data"
    assert result.n_pairs_used == 2  # honestly reported, even though discarded
    artifact = result.artifact
    assert all(c.coefficient == 0.0 for c in artifact.coefficients)
    assert all(c.robust_center == 0.0 and c.robust_scale == 1.0 for c in artifact.coefficients)
    assert all(cal.offset == 0.0 and cal.shrinkage_weight == 0.0 for cal in artifact.role_calibration.values())
    assert artifact.score_calibration["scale"] == 5.0  # the fixed neutral default
    assert artifact.intercept == 0.0


def test_train_tier_exploratory_fit_only_when_threshold_explicitly_lowered():
    records = tuple(
        _record(game_id, kills=k, deaths=d)
        for game_id, (k, d) in enumerate([(9, 0), (0, 9), (8, 1), (1, 8)], start=1)
    )
    pairs = (_pair("1:1", "2:1", "left"), _pair("3:1", "4:1", "left"))
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    # Same data, but the caller explicitly lowers the threshold below the
    # real usable-pair count (2) -- now a real, nonzero exploratory fit is
    # allowed through.
    result = train_tier(
        dataset, "match_v5", model_version="0.0.1-dev", feature_version="2.0.0-evidence",
        calibration_version="0.0.1-dev", min_pairs_for_nontrivial_fit=2,
    )
    assert result.status == "fitted"
    assert result.artifact.coefficients[0].coefficient != 0.0 or any(
        c.coefficient != 0.0 for c in result.artifact.coefficients
    )


# ── 3. sufficient data ───────────────────────────────────────────────────────

def test_train_tier_fitted_status_with_enough_pairs():
    records = tuple(
        _record(game_id, kills=k, deaths=d)
        for game_id, (k, d) in enumerate(
            [(9, 0), (0, 9), (8, 1), (1, 8), (7, 1), (1, 7), (8, 0), (0, 8),
             (9, 1), (1, 9), (7, 0), (0, 7)], start=1,
        )
    )
    pairs = tuple(
        _pair(f"{a}:1", f"{b}:1", "left")
        for a, b in [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10), (11, 12)]
    )
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    result = train_tier(
        dataset, "match_v5", model_version="0.0.1-dev", feature_version="2.0.0-evidence",
        calibration_version="0.0.1-dev", min_pairs_for_nontrivial_fit=5,
    )
    assert result.status == "fitted"
    assert result.artifact.production_ready is False  # export never sets this True
    coefficient_by_name = {c.spec.name: c.coefficient for c in result.artifact.coefficients}
    assert coefficient_by_name["raw_kills"] > 0.0
    assert coefficient_by_name["raw_deaths"] < 0.0


def test_train_tier_passes_include_abstained_through():
    normal = _record(1, kills=8, deaths=1)
    abstained = _record(2, kills=1, deaths=8, abstain=True)
    pair = _pair("1:1", "2:1", "left")
    dataset = TrainingDataset(
        schema_version=1, feature_records=(normal, abstained), pair_labels=(pair,),
    )
    excluded_result = train_tier(
        dataset, "match_v5", model_version="0.0.1-dev", feature_version="2.0.0-evidence",
        calibration_version="0.0.1-dev", min_pairs_for_nontrivial_fit=1,
    )
    assert excluded_result.n_pairs_used == 0  # abstained ref excluded by default

    included_result = train_tier(
        dataset, "match_v5", model_version="0.0.1-dev", feature_version="2.0.0-evidence",
        calibration_version="0.0.1-dev", min_pairs_for_nontrivial_fit=1,
        include_abstained=True,
    )
    assert included_result.n_pairs_used == 1


# ── 4. train_all_tiers only trains tiers present ────────────────────────────

def test_train_all_tiers_skips_absent_tiers():
    records = (_record(1, 8, 1, tier="match_v5"),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    results = train_all_tiers(
        dataset, model_version="0.0.1-dev", feature_version="2.0.0-evidence",
        calibration_version="0.0.1-dev",
    )
    assert set(results) == {"match_v5"}


def test_train_all_tiers_trains_every_present_tier():
    records = (
        _record(1, 8, 1, tier="match_v5"),
        _record(2, 8, 1, tier="lcu_timeline"),
        _record(3, 8, 1, tier="aggregate"),
    )
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    results = train_all_tiers(
        dataset, model_version="0.0.1-dev", feature_version="2.0.0-evidence",
        calibration_version="0.0.1-dev",
    )
    assert set(results) == {"match_v5", "lcu_timeline", "aggregate"}
    for evidence_source, result in results.items():
        assert result.artifact.evidence_source == evidence_source


# ── 5. tier-specific feature contract ────────────────────────────────────────

def test_train_tier_uses_the_tier_specific_feature_contract():
    records = (_record(1, 8, 1, tier="aggregate"),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    result = train_tier(
        dataset, "aggregate", model_version="0.0.1-dev", feature_version="2.0.0-evidence",
        calibration_version="0.0.1-dev",
    )
    names = {c.spec.name for c in result.artifact.coefficients}
    assert names == {spec.name for spec in feature_contract_for_tier("aggregate")}
    assert names == {"raw_kills", "raw_deaths", "raw_assists"}
