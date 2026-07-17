"""Tests for score_v2/training/boosting.py -- the monotonic additive
boosting baseline.

Sections:
  1. Zero-pair training: honest neutral (empty ensemble) result.
  2. Monotonic invariant: every fitted stump's low/high values always
     respect its feature's declared direction, even against adversarial
     labels.
  3. Determinism.
  4. Pairwise correction reduces loss on separable synthetic data.
  5. Honest stopping: `stopped_reason` reflects a real criterion; a
     validation-based early stop mechanism is genuinely exercised.
  6. Single-tier assertion + abstain exclusion.
  7. Monotonic counterfactual invariant on a FITTED ensemble.
  8. `derive_inner_early_stop_split`: grouped by connected game-id
     components (no pair ever straddles the inner boundary),
     deterministic across repeated calls, and honestly disabled
     (`(None, None)`) when there are too few independent groups.
"""

from score_v2.feature_spec import DIRECTION_NEGATIVE, DIRECTION_POSITIVE, FEATURE_ALLOWLIST, extract_feature_vector
from score_v2.model_shapes import evaluate_boosted_stumps
from score_v2.training.boosting import derive_inner_early_stop_split, fit_pairwise_boosted_stumps
from score_v2.training.dataset import DatasetValidationError, PairLabel, TrainingDataset, build_feature_record


def _gf(kills, deaths, assists, abstain=False):
    return {
        "duration_seconds": 1800.0, "abstain": abstain, "abstain_reason": None,
        "chosen_source_completeness": 1.0,
        "participants": {"1": {
            "raw": {"kills": kills, "deaths": deaths, "assists": assists},
            "baseline": {"role": "mid", "champion": "TestChamp", "patch": "14.1"},
        }},
    }


def _record(game_id, kills, deaths, assists=3, split="train", evidence_source="match_v5", abstain=False):
    return build_feature_record(
        game_id=game_id, participant_id=1, evidence_source=evidence_source,
        features_for_game=_gf(kills, deaths, assists, abstain=abstain), split=split,
    )


def _pair(left_ref, right_ref, choice="left", confidence=0.9):
    if choice == "left":
        winner_ref, relation = left_ref, "left_preferred"
    elif choice == "right":
        winner_ref, relation = right_ref, "right_preferred"
    else:
        winner_ref, relation = None, choice
    return PairLabel(
        pair_id=f"{left_ref}|{right_ref}", left_ref=left_ref, right_ref=right_ref,
        winner_ref=winner_ref, relation=relation, choice=choice, confidence=confidence,
        rationale_tags=("combat_impact",), reviewer_id="r1",
        created_at="2026-01-01T00:00:00+00:00",
    )


# ── 1. zero-pair training is honestly neutral ───────────────────────────────

def test_zero_pairs_yields_empty_ensemble():
    records = (_record(1, kills=8, deaths=1, assists=2),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    fitted = fit_pairwise_boosted_stumps(dataset)
    assert fitted.stumps == ()
    assert fitted.rounds_run == 0
    assert fitted.n_pairs_used == 0
    assert fitted.final_loss is None


# ── 2. monotonic invariant ───────────────────────────────────────────────────

def test_fitted_stumps_always_respect_declared_direction_even_against_noise():
    records = tuple(
        _record(game_id, kills=k, deaths=d, assists=3)
        for game_id, (k, d) in enumerate([
            (8, 1), (1, 8), (7, 2), (2, 7), (6, 3), (3, 6), (5, 4), (4, 5),
        ], start=1)
    )
    pairs = tuple(
        _pair(f"{a}:1", f"{b}:1", "right") for a, b in zip(range(1, 9), range(2, 9))
    )
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    fitted = fit_pairwise_boosted_stumps(dataset, n_rounds=30)
    specs_by_name = {spec.name: spec for spec in FEATURE_ALLOWLIST}
    for stump in fitted.stumps:
        direction = specs_by_name[stump.spec.name].direction
        if direction == DIRECTION_POSITIVE:
            assert stump.low_value <= stump.high_value + 1e-9, stump.spec.name
        elif direction == DIRECTION_NEGATIVE:
            assert stump.low_value >= stump.high_value - 1e-9, stump.spec.name


# ── 3. determinism ───────────────────────────────────────────────────────────

def test_fit_pairwise_boosted_stumps_is_deterministic():
    records = tuple(
        _record(game_id, kills=k, deaths=d, assists=3)
        for game_id, (k, d) in enumerate([(8, 1), (1, 8), (6, 2), (2, 6)], start=1)
    )
    pairs = (_pair("1:1", "2:1", "left"), _pair("3:1", "4:1", "left"))
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)

    fitted_a = fit_pairwise_boosted_stumps(dataset, n_rounds=25)
    fitted_b = fit_pairwise_boosted_stumps(dataset, n_rounds=25)
    assert len(fitted_a.stumps) == len(fitted_b.stumps)
    for stump_a, stump_b in zip(fitted_a.stumps, fitted_b.stumps):
        assert stump_a == stump_b


# ── 4. pairwise correction reduces loss on separable data ───────────────────

def test_training_reduces_loss_on_separable_synthetic_data():
    records = tuple(
        _record(game_id, kills=k, deaths=d, assists=3)
        for game_id, (k, d) in enumerate(
            [(9, 0), (0, 9), (8, 1), (1, 8), (7, 1), (1, 7), (8, 0), (0, 8)], start=1,
        )
    )
    pairs = tuple(
        _pair(f"{a}:1", f"{b}:1", "left") for a, b in [(1, 2), (3, 4), (5, 6), (7, 8)]
    )
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    fitted = fit_pairwise_boosted_stumps(dataset, n_rounds=60)
    assert fitted.final_loss is not None
    assert fitted.final_loss < 0.4
    assert len(fitted.stumps) > 0


# ── 5. honest stopping ───────────────────────────────────────────────────────

def test_stopped_reason_iteration_budget_on_zero_pair_data():
    records = (_record(1, kills=8, deaths=1, assists=2),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    fitted = fit_pairwise_boosted_stumps(dataset, n_rounds=50)
    # Zero usable pairs: the round loop never even executes.
    assert fitted.rounds_run == 0
    assert fitted.stopped_reason == "iteration_budget_exhausted"


def test_validation_based_early_stopping_reports_a_real_mechanism():
    # Training data with a real but NOISY signal (a few adversarial
    # flips) -- enough rounds risks overfitting the noise, which the
    # validation split (a CLEAN version of the same signal) can catch.
    train_records = tuple(
        _record(game_id, kills=k, deaths=1, assists=3)
        for game_id, k in enumerate(range(0, 20), start=1)
    )
    train_pairs = []
    for a in range(1, 20):
        b = a + 1
        ka = train_records[a - 1].features["raw"]["kills"]
        kb = train_records[b - 1].features["raw"]["kills"]
        choice_correct = "left" if ka > kb else "right"
        # Inject noise on every 5th pair (flip the label).
        choice = choice_correct if a % 5 != 0 else ("right" if choice_correct == "left" else "left")
        train_pairs.append(_pair(f"{a}:1", f"{b}:1", choice))
    train_dataset = TrainingDataset(
        schema_version=1, feature_records=train_records, pair_labels=tuple(train_pairs),
    )

    validation_records = tuple(
        _record(100 + game_id, kills=k, deaths=1, assists=3, split="validation")
        for game_id, k in enumerate(range(0, 20), start=1)
    )
    validation_pairs = tuple(
        _pair(
            f"{100 + a}:1", f"{100 + a + 1}:1",
            "left" if validation_records[a - 1].features["raw"]["kills"]
            > validation_records[a].features["raw"]["kills"] else "right",
        )
        for a in range(1, 20)
    )
    validation_dataset = TrainingDataset(
        schema_version=1, feature_records=validation_records, pair_labels=validation_pairs,
    )

    fitted = fit_pairwise_boosted_stumps(
        train_dataset, n_rounds=300, shrinkage=0.3, min_gain=1e-12,
        loss_tolerance=1e-12, validation_dataset=validation_dataset, validation_patience=3,
    )
    # Whatever mechanism stopped it, it must be a REAL, documented one --
    # never a fabricated "converged because pairs existed".
    assert fitted.stopped_reason in (
        "validation_plateau", "no_further_gain", "train_loss_plateau",
        "iteration_budget_exhausted",
    )
    assert fitted.rounds_run > 0
    assert fitted.best_validation_loss is not None


def test_validation_early_stopping_can_restore_the_zero_stump_baseline():
    train_records = tuple(
        _record(game_id, kills=kills, deaths=0)
        for game_id, kills in enumerate([9, 0, 8, 1, 7, 2, 6, 3], start=1)
    )
    train_pairs = tuple(
        _pair(f"{left}:1", f"{right}:1", "left")
        for left, right in ((1, 2), (3, 4), (5, 6), (7, 8))
    )
    train_dataset = TrainingDataset(
        schema_version=1, feature_records=train_records, pair_labels=train_pairs,
    )

    validation_records = tuple(
        _record(100 + game_id, kills=kills, deaths=0, split="validation")
        for game_id, kills in enumerate([9, 0, 8, 1, 7, 2, 6, 3], start=1)
    )
    validation_pairs = tuple(
        _pair(f"{100 + left}:1", f"{100 + right}:1", "right")
        for left, right in ((1, 2), (3, 4), (5, 6), (7, 8))
    )
    validation_dataset = TrainingDataset(
        schema_version=1,
        feature_records=validation_records,
        pair_labels=validation_pairs,
    )

    fitted = fit_pairwise_boosted_stumps(
        train_dataset, validation_dataset=validation_dataset,
        n_rounds=30, validation_patience=2, loss_tolerance=0.0,
    )
    assert fitted.best_validation_loss is not None
    assert fitted.stumps == ()


def test_validation_with_no_usable_pairs_keeps_the_trained_ensemble():
    train_records = tuple(
        _record(game_id, kills=kills, deaths=0)
        for game_id, kills in enumerate([9, 0, 8, 1, 7, 2, 6, 3], start=1)
    )
    train_pairs = tuple(
        _pair(f"{left}:1", f"{right}:1", "left")
        for left, right in ((1, 2), (3, 4), (5, 6), (7, 8))
    )
    train_dataset = TrainingDataset(
        schema_version=1, feature_records=train_records, pair_labels=train_pairs,
    )
    validation_records = (
        _record(101, kills=9, deaths=0, split="validation", abstain=True),
        _record(102, kills=0, deaths=9, split="validation"),
    )
    validation_dataset = TrainingDataset(
        schema_version=1,
        feature_records=validation_records,
        pair_labels=(_pair("101:1", "102:1", "left"),),
    )

    fitted = fit_pairwise_boosted_stumps(
        train_dataset, validation_dataset=validation_dataset, n_rounds=10,
    )
    assert fitted.best_validation_loss is None
    assert fitted.stumps


# ── 6. single-tier assertion + abstain exclusion ────────────────────────────

def test_fit_pairwise_boosted_stumps_rejects_mixed_tier_dataset():
    r1 = _record(1, 8, 1, 2, evidence_source="match_v5")
    r2 = _record(2, 8, 1, 2, evidence_source="aggregate")
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=())
    try:
        fit_pairwise_boosted_stumps(dataset)
        assert False, "expected DatasetValidationError"
    except DatasetValidationError:
        pass


def test_abstained_records_excluded_from_boosting_training_by_default():
    normal = _record(1, kills=8, deaths=1, assists=2, abstain=False)
    abstained = _record(2, kills=8, deaths=1, assists=2, abstain=True)
    pair = _pair("1:1", "2:1", "left")
    dataset = TrainingDataset(
        schema_version=1, feature_records=(normal, abstained), pair_labels=(pair,),
    )
    fitted = fit_pairwise_boosted_stumps(dataset)
    assert fitted.n_items == 1
    assert fitted.n_items_excluded_abstain == 1
    assert fitted.n_pairs_used == 0


# ── 7. monotonic counterfactual invariant on a FITTED ensemble ─────────────

def test_fitted_boosting_counterfactual_never_regresses_for_positive_feature():
    records = tuple(
        _record(game_id, kills=k, deaths=1, assists=3)
        for game_id, k in enumerate([9, 0, 8, 1, 7, 2, 6, 3], start=1)
    )
    pairs = tuple(_pair(f"{a}:1", f"{b}:1", "left") for a, b in [(1, 2), (3, 4), (5, 6), (7, 8)])
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    fitted = fit_pairwise_boosted_stumps(dataset, n_rounds=60)

    base_features = _gf(kills=3, deaths=1, assists=3)["participants"]["1"]
    scores = []
    for kills in range(0, 12):
        features = {**base_features, "raw": {**base_features["raw"], "kills": kills}}
        vector = extract_feature_vector(features, specs=fitted.specs)
        scores.append(evaluate_boosted_stumps(fitted.stumps, vector))
    assert all(scores[i] <= scores[i + 1] + 1e-9 for i in range(len(scores) - 1))


# ── 8. derive_inner_early_stop_split ────────────────────────────────────────

def _independent_pair_dataset(n_games=40, split="train"):
    """`n_games` records, paired up as (1,2), (3,4), (5,6), ... -- each
    game participates in at most ONE pair, so the connected-component
    grouping yields `n_games // 2` independent 2-game groups (a realistic
    "many small, unrelated comparisons" shape, as opposed to one long
    transitively-connected chain).
    """
    records = tuple(
        _record(game_id, kills=k, deaths=0, split=split)
        for game_id, k in enumerate(range(0, n_games), start=1)
    )
    pairs = tuple(
        _pair(f"{a}:1", f"{b}:1", "left")
        for a, b in zip(range(1, n_games, 2), range(2, n_games + 1, 2))
    )
    return TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)


def test_derive_inner_early_stop_split_is_deterministic():
    dataset = _independent_pair_dataset()
    fit_a, stop_a = derive_inner_early_stop_split(dataset)
    fit_b, stop_b = derive_inner_early_stop_split(dataset)
    assert [r.game_id for r in fit_a.feature_records] == [r.game_id for r in fit_b.feature_records]
    assert [r.game_id for r in stop_a.feature_records] == [r.game_id for r in stop_b.feature_records]


def test_derive_inner_early_stop_split_has_no_leaked_pairs():
    dataset = _independent_pair_dataset()
    fit_dataset, stop_dataset = derive_inner_early_stop_split(dataset)
    assert fit_dataset is not None and stop_dataset is not None
    fit_refs = {r.base_ref for r in fit_dataset.feature_records}
    stop_refs = {r.base_ref for r in stop_dataset.feature_records}
    assert fit_refs.isdisjoint(stop_refs)
    # Every ORIGINAL pair must be either wholly inside fit, wholly inside
    # stop, or (if a game got excluded from both, which should not happen
    # here) excluded entirely -- never split across the boundary.
    for pair in dataset.pair_labels:
        left_in_fit, right_in_fit = pair.left_ref in fit_refs, pair.right_ref in fit_refs
        left_in_stop, right_in_stop = pair.left_ref in stop_refs, pair.right_ref in stop_refs
        assert left_in_fit == right_in_fit, f"leaked pair (fit side): {pair.pair_id}"
        assert left_in_stop == right_in_stop, f"leaked pair (stop side): {pair.pair_id}"
    # Every pair kept must be entirely resolvable in exactly one dataset.
    assert set(fit_dataset.pair_labels) <= set(dataset.pair_labels)
    assert set(stop_dataset.pair_labels) <= set(dataset.pair_labels)
    assert set(fit_dataset.pair_labels).isdisjoint(set(stop_dataset.pair_labels))


def test_derive_inner_early_stop_split_never_crosses_a_pair_even_when_games_are_chained():
    # A fully chained dataset (pair(1,2), pair(2,3), pair(3,4), ...)
    # transitively unions EVERY game into one connected component -- there
    # is no way to carve out a non-trivial inner_stop without splitting a
    # pair, so this must be honestly disabled, not forced.
    n = 20
    records = tuple(_record(i, kills=k, deaths=0) for i, k in enumerate(range(0, n), start=1))
    pairs = tuple(_pair(f"{a}:1", f"{b}:1", "left") for a, b in zip(range(1, n), range(2, n + 1)))
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    fit_dataset, stop_dataset = derive_inner_early_stop_split(dataset)
    assert fit_dataset is None and stop_dataset is None


def test_derive_inner_early_stop_split_disabled_for_tiny_train():
    records = (_record(1, kills=9, deaths=0), _record(2, kills=0, deaths=9))
    pairs = (_pair("1:1", "2:1", "left"),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    fit_dataset, stop_dataset = derive_inner_early_stop_split(dataset)
    assert fit_dataset is None and stop_dataset is None


def test_derive_inner_early_stop_split_reserves_at_least_one_group_for_fit():
    dataset = _independent_pair_dataset(n_games=8)  # exactly 4 groups (min_groups default)
    fit_dataset, stop_dataset = derive_inner_early_stop_split(dataset)
    assert fit_dataset is not None and stop_dataset is not None
    assert len(fit_dataset.feature_records) > 0
    assert len(stop_dataset.feature_records) > 0
    assert len(fit_dataset.feature_records) + len(stop_dataset.feature_records) == 8


def test_derive_inner_early_stop_split_fit_and_stop_disjoint_and_using_only_train_data():
    # Sanity: fit_dataset/stop_dataset together must be a SUBSET of the
    # original train_dataset's own records -- nothing invented, nothing
    # borrowed from anywhere else.
    dataset = _independent_pair_dataset()
    fit_dataset, stop_dataset = derive_inner_early_stop_split(dataset)
    original_refs = {r.base_ref for r in dataset.feature_records}
    combined_refs = {r.base_ref for r in fit_dataset.feature_records} | {
        r.base_ref for r in stop_dataset.feature_records
    }
    assert combined_refs <= original_refs


def test_derive_inner_early_stop_split_rejects_abstain_only_monitor_pairs():
    dataset = _independent_pair_dataset()
    _, initial_stop = derive_inner_early_stop_split(
        dataset, include_abstained=True,
    )
    stop_refs = {record.base_ref for record in initial_stop.feature_records}
    records = tuple(
        _record(
            record.game_id,
            kills=record.features["raw"]["kills"],
            deaths=record.features["raw"]["deaths"],
            abstain=record.base_ref in stop_refs,
        )
        for record in dataset.feature_records
    )
    abstain_partitioned = TrainingDataset(
        schema_version=1, feature_records=records,
        pair_labels=dataset.pair_labels,
    )

    fit_dataset, stop_dataset = derive_inner_early_stop_split(
        abstain_partitioned, include_abstained=False,
    )
    assert fit_dataset is None and stop_dataset is None
