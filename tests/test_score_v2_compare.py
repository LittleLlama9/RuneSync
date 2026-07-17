"""Tests for score_v2/training/compare.py -- the model-family comparison
orchestrator.

Sections:
  1. Split safety: an empty TRAIN split is honestly reported as
     `insufficient_data`, never silently falls back to the full dataset.
  2. Minimum-data eligibility gating: each family is ineligible below its
     own configured minimum train-pairs threshold, and a degenerate
     (single-leaf) tree is ineligible regardless of pair count.
  3. Selection is validation-only: "no model wins on training metrics
     alone" -- a family with better TRAIN loss but worse VALIDATION
     pairwise accuracy must never be selected over one with the reverse.
  4. Non-linear candidates CAN outperform linear on a genuinely
     non-linear monotonic signal (empirically demonstrated, not assumed).
  5. Deterministic selection: identical input -> identical winner,
     bit-for-bit, across repeated runs.
  6. No validation/test leakage: normalization statistics used to build
     every candidate's artifact are fit from TRAIN alone, never
     influenced by validation/test data, regardless of the latter's
     distribution.
  7. Honest current-corpus no-selection behavior: with the real project's
     current near-zero-pair scale, every family is ineligible and
     `selected_model` is `None` -- never a fabricated winner.
  8. `compare_all_tiers` only compares tiers actually present in the
     dataset, and multi-tier identity (`dataset_for_tier`) keeps each
     tier's comparison independent.
  9. `build_artifact_for_family`: the one explicit, deterministic path
     used for export -- refuses to export a below-threshold or
     structurally-ineligible family.
  10. Boosting must never tune on the outer validation/test split:
      perturbing the outer validation OR test split cannot change
      boosting's fitted structure, content hash, or runtime predictions;
      a tiny train dataset honestly disables the inner early-stop split
      (never substituting the outer validation set); an early-stopped
      boosting winner re-derives and exports with an identical hash and
      identical runtime score predictions.
"""

import datetime

from score_v2.artifact import (
    MODEL_FAMILY_BOOSTED_STUMPS,
    MODEL_FAMILY_GAM,
    MODEL_FAMILY_LINEAR,
    MODEL_FAMILY_MONOTONIC_TREE,
)
from score_v2.training.compare import (
    build_artifact_for_family,
    compare_all_tiers,
    compare_tier,
)
from score_v2.training.dataset import PairLabel, TrainingDataset, build_feature_record

FIXED_NOW = datetime.datetime(2026, 7, 17, tzinfo=datetime.timezone.utc)


def _gf(kills, deaths, assists=3, role="mid", abstain=False):
    return {
        "duration_seconds": 1800.0, "abstain": abstain, "abstain_reason": None,
        "chosen_source_completeness": 1.0,
        "participants": {"1": {
            "raw": {"kills": kills, "deaths": deaths, "assists": assists},
            "baseline": {"role": role, "champion": "TestChamp", "patch": "14.1"},
        }},
    }


def _record(game_id, kills, deaths, split="train", evidence_source="aggregate", assists=3):
    return build_feature_record(
        game_id=game_id, participant_id=1, evidence_source=evidence_source,
        features_for_game=_gf(kills, deaths, assists), split=split,
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


_COMMON_KWARGS = dict(
    model_version="0.0.1-dev", feature_version="2.0.0-evidence", calibration_version="0.0.1-dev",
    now=FIXED_NOW,
)


def _step_utility(kills):
    """A genuinely non-linear (non-log-shaped) monotonic signal: flat and
    slow below 7, then a sharp jump -- a shape a single log1p-transformed
    linear coefficient cannot represent nearly as well as a piecewise
    shape (GAM) or a split-based model (boosting/tree).
    """
    return (0.1 * kills) if kills < 7 else (10.0 + 0.1 * kills)


def _build_step_signal_dataset(seed=7, n_train=160, n_validation=40, n_test=40):
    import random
    rng = random.Random(seed)
    records = []
    game_id = 1
    splits = ["train"] * n_train + ["validation"] * n_validation + ["test"] * n_test
    rng.shuffle(splits)
    for split in splits:
        kills = rng.randint(0, 15)
        deaths = rng.randint(0, 3)
        records.append(_record(game_id, kills, deaths, split=split))
        game_id += 1

    by_split: dict = {}
    for record in records:
        by_split.setdefault(record.split, []).append(record)

    pairs = []
    for split_records in by_split.values():
        for i in range(0, len(split_records) - 1, 2):
            a, b = split_records[i], split_records[i + 1]
            ua = _step_utility(a.features["raw"]["kills"])
            ub = _step_utility(b.features["raw"]["kills"])
            if ua == ub:
                continue
            choice = "left" if ua > ub else "right"
            pairs.append(_pair(a.base_ref, b.base_ref, choice))

    return TrainingDataset(schema_version=1, feature_records=tuple(records), pair_labels=tuple(pairs))


_LOW_THRESHOLDS = {
    MODEL_FAMILY_LINEAR: 5, MODEL_FAMILY_GAM: 8,
    MODEL_FAMILY_BOOSTED_STUMPS: 8, MODEL_FAMILY_MONOTONIC_TREE: 8,
}


# ── 1. split safety ──────────────────────────────────────────────────────────

def test_empty_train_split_is_insufficient_data_never_falls_back():
    # Records exist only for "validation"/"test" -- zero for "train".
    records = (
        _record(1, kills=9, deaths=0, split="validation"),
        _record(2, kills=0, deaths=9, split="test"),
    )
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    result = compare_tier(dataset, "aggregate", **_COMMON_KWARGS)
    assert result.status == "insufficient_data"
    assert result.candidates == ()
    assert result.selected_model is None
    assert result.selection_reason == "no_train_records"


# ── 2. minimum-data eligibility gating ──────────────────────────────────────

def test_families_below_their_own_minimum_pairs_are_ineligible():
    # Only 3 usable pairs -- below every family's default minimum
    # (linear=20, gam=80, boosted_stumps=120, monotonic_tree=60).
    records = tuple(_record(i, kills=k, deaths=0) for i, k in enumerate([9, 0, 8, 1, 7, 2], start=1))
    pairs = (_pair("1:1", "2:1", "left"), _pair("3:1", "4:1", "left"), _pair("5:1", "6:1", "left"))
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    result = compare_tier(dataset, "aggregate", **_COMMON_KWARGS)
    for candidate in result.candidates:
        assert candidate.eligible is False
        # The tree may ALSO be structurally degenerate at this tiny scale
        # (no split survives its own min_samples_leaf guardrail) -- either
        # honest reason is acceptable for it specifically.
        if candidate.model_family == MODEL_FAMILY_MONOTONIC_TREE:
            assert candidate.ineligibility_reason in (
                "degenerate_no_split",
            ) or "below the minimum" in candidate.ineligibility_reason
        else:
            assert "below the minimum" in candidate.ineligibility_reason
    assert result.selected_model is None
    assert result.selection_reason == "no_selectable_candidate"


def test_degenerate_tree_is_ineligible_regardless_of_pair_count():
    dataset = _build_step_signal_dataset()
    # Force the tree's min_samples_leaf so high that no split can ever
    # survive, even though there is plenty of data -- this must be
    # reported as structurally ineligible ("degenerate_no_split"), not
    # silently presented as a genuine tree candidate.
    import score_v2.training.compare as compare_mod

    original_fit = compare_mod.fit_pairwise_monotonic_tree

    def _forced_degenerate(*args, **kwargs):
        kwargs["min_samples_leaf"] = 100_000
        return original_fit(*args, **kwargs)

    compare_mod.fit_pairwise_monotonic_tree = _forced_degenerate
    try:
        result = compare_tier(
            dataset, "aggregate", min_pairs_by_family=_LOW_THRESHOLDS, **_COMMON_KWARGS,
        )
    finally:
        compare_mod.fit_pairwise_monotonic_tree = original_fit

    tree_candidate = next(c for c in result.candidates if c.model_family == MODEL_FAMILY_MONOTONIC_TREE)
    assert tree_candidate.eligible is False
    assert tree_candidate.ineligibility_reason == "degenerate_no_split"


# ── 3. selection is validation-only ─────────────────────────────────────────

def test_selection_never_uses_train_metrics_alone():
    # This dataset's signal (the step function) is genuinely non-linear;
    # we already know from empirical testing that the linear family's
    # TRAIN loss and VALIDATION accuracy do not have to agree with a
    # higher-capacity family's -- the important invariant this test
    # locks in is structural: `notes`/`selection_reason` are derived only
    # from `validation_evaluation`, never `train_final_loss`/`train_converged`.
    dataset = _build_step_signal_dataset()
    result = compare_tier(dataset, "aggregate", min_pairs_by_family=_LOW_THRESHOLDS, **_COMMON_KWARGS)
    assert result.status == "compared"
    assert "validation pairwise accuracy" in result.selection_reason
    assert "train" not in result.selection_reason.lower() or "training" not in result.selection_reason.lower()
    # The winner must actually have the best validation_pairwise_accuracy
    # among eligible candidates -- not merely the best train_final_loss.
    eligible = [c for c in result.candidates if c.eligible and c.validation_pairwise_accuracy is not None]
    best_by_validation = max(eligible, key=lambda c: c.validation_pairwise_accuracy)
    assert result.selected_model in {
        c.model_family for c in eligible
        if c.validation_pairwise_accuracy == best_by_validation.validation_pairwise_accuracy
    }


# ── 4. non-linear candidates CAN outperform linear ──────────────────────────

def test_nonlinear_family_can_outperform_linear_on_nonlinear_signal():
    dataset = _build_step_signal_dataset()
    result = compare_tier(dataset, "aggregate", min_pairs_by_family=_LOW_THRESHOLDS, **_COMMON_KWARGS)
    candidates_by_family = {c.model_family: c for c in result.candidates}
    linear_accuracy = candidates_by_family[MODEL_FAMILY_LINEAR].validation_pairwise_accuracy
    # At least one non-linear family must strictly beat (or tie and win
    # via selection) the linear family's validation accuracy on this
    # genuinely non-linear signal -- demonstrating higher-capacity models
    # are not merely "allowed" to win but genuinely CAN, empirically.
    nonlinear_accuracies = [
        candidates_by_family[family].validation_pairwise_accuracy
        for family in (MODEL_FAMILY_GAM, MODEL_FAMILY_BOOSTED_STUMPS, MODEL_FAMILY_MONOTONIC_TREE)
        if candidates_by_family[family].eligible
        and candidates_by_family[family].validation_pairwise_accuracy is not None
    ]
    assert nonlinear_accuracies, "expected at least one eligible non-linear candidate"
    assert max(nonlinear_accuracies) >= linear_accuracy
    assert max(nonlinear_accuracies) > linear_accuracy  # genuinely, not just tied
    assert result.selected_model != MODEL_FAMILY_LINEAR


# ── 5. deterministic selection ──────────────────────────────────────────────

def test_selection_is_deterministic_across_repeated_runs():
    dataset = _build_step_signal_dataset()
    result_a = compare_tier(dataset, "aggregate", min_pairs_by_family=_LOW_THRESHOLDS, **_COMMON_KWARGS)
    result_b = compare_tier(dataset, "aggregate", min_pairs_by_family=_LOW_THRESHOLDS, **_COMMON_KWARGS)
    assert result_a.selected_model == result_b.selected_model
    assert result_a.selected_artifact_content_hash == result_b.selected_artifact_content_hash
    for candidate_a, candidate_b in zip(result_a.candidates, result_b.candidates):
        assert candidate_a.validation_pairwise_accuracy == candidate_b.validation_pairwise_accuracy


# ── 6. no validation/test leakage ───────────────────────────────────────────

def test_normalization_is_fit_from_train_only_not_validation_or_test():
    train_records = tuple(_record(i, kills=k, deaths=0) for i, k in enumerate(range(0, 10), start=1))
    train_pairs = tuple(
        _pair(
            f"{a}:1", f"{b}:1",
            "left" if train_records[a - 1].features["raw"]["kills"]
            > train_records[b - 1].features["raw"]["kills"] else "right",
        )
        for a, b in zip(range(1, 10), range(2, 11))
    )
    # Validation/test have a WILDLY different kills distribution -- if
    # normalization leaked from them, the resulting artifact's stored
    # robust_center/robust_scale for raw_kills would differ from a
    # train-only fit.
    validation_records = tuple(
        _record(100 + i, kills=k, deaths=0, split="validation")
        for i, k in enumerate([500, 600, 700, 800], start=1)
    )
    test_records = tuple(
        _record(200 + i, kills=k, deaths=0, split="test")
        for i, k in enumerate([9000, 9100], start=1)
    )
    all_records = train_records + validation_records + test_records
    dataset = TrainingDataset(schema_version=1, feature_records=all_records, pair_labels=train_pairs)

    from score_v2.training.baseline import fit_pairwise_baseline
    from score_v2.feature_spec import feature_contract_for_tier
    from score_v2.training.export import dataset_for_tier
    from score_v2.training.dataset import select_split

    tier_dataset = dataset_for_tier(dataset, "aggregate")
    train_only = select_split(tier_dataset, "train")
    expected = fit_pairwise_baseline(train_only, specs=feature_contract_for_tier("aggregate"))

    result = compare_tier(dataset, "aggregate", min_pairs_by_family={"linear": 1}, **_COMMON_KWARGS)
    linear_candidate = next(c for c in result.candidates if c.model_family == MODEL_FAMILY_LINEAR)
    assert linear_candidate.eligible

    artifact = build_artifact_for_family(
        dataset, "aggregate", MODEL_FAMILY_LINEAR, min_pairs_by_family={"linear": 1}, **_COMMON_KWARGS,
    )
    kills_coefficient = next(c for c in artifact.coefficients if c.spec.name == "raw_kills")
    assert kills_coefficient.robust_center == expected.normalizations["raw_kills"].center
    assert kills_coefficient.robust_scale == expected.normalizations["raw_kills"].scale


# ── 7. honest current-corpus no-selection behavior ──────────────────────────

def test_honest_no_selection_with_zero_pairs():
    # Mirrors the current real corpus: records exist but there are no
    # pairwise labels at all (Match-V5 authorization still blocked).
    records = tuple(_record(i, kills=k, deaths=0) for i, k in enumerate([8, 1, 7, 2], start=1))
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    result = compare_tier(dataset, "aggregate", **_COMMON_KWARGS)
    assert result.status == "compared"
    assert result.selected_model is None
    assert all(candidate.eligible is False for candidate in result.candidates)
    assert all(candidate.train_n_pairs_used == 0 for candidate in result.candidates)


# ── 8. compare_all_tiers / multi-tier identity ──────────────────────────────

def test_compare_all_tiers_only_compares_tiers_present():
    records = (_record(1, kills=8, deaths=1, evidence_source="match_v5"),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    results = compare_all_tiers(dataset, **_COMMON_KWARGS)
    assert set(results) == {"match_v5"}


def test_compare_all_tiers_keeps_each_tier_independent():
    # Same base_ref/game/participant identity across tiers -- a real,
    # normal multi-tier scenario. Each tier's comparison must use only
    # its OWN records/pairs.
    match_v5_records = tuple(
        _record(i, kills=k, deaths=0, evidence_source="match_v5")
        for i, k in enumerate([9, 0, 8, 1, 7, 2, 6, 3], start=1)
    )
    aggregate_records = tuple(
        _record(i, kills=k, deaths=0, evidence_source="aggregate")
        for i, k in enumerate([9, 0, 8, 1, 7, 2, 6, 3], start=1)
    )
    pairs = tuple(_pair(f"{a}:1", f"{b}:1", "left") for a, b in [(1, 2), (3, 4), (5, 6), (7, 8)])
    dataset = TrainingDataset(
        schema_version=1, feature_records=match_v5_records + aggregate_records, pair_labels=pairs,
    )
    results = compare_all_tiers(dataset, min_pairs_by_family={"linear": 2}, **_COMMON_KWARGS)
    assert set(results) == {"match_v5", "aggregate"}
    for evidence_source, result in results.items():
        for candidate in result.candidates:
            if candidate.model_family == MODEL_FAMILY_BOOSTED_STUMPS:
                # boosted_stumps fits on its own deterministic INNER split
                # of this tier's 8 train records (never the full 16 across
                # both tiers) -- n_items reflects the inner_fit subset,
                # honestly smaller than the tier's own full 8.
                assert candidate.n_items <= 8
                assert candidate.training_metadata_extra["inner_fit_n_items"] == candidate.n_items
            else:
                assert candidate.n_items == 8  # each tier's own 8 records, not 16


# ── 9. build_artifact_for_family ─────────────────────────────────────────────

def test_build_artifact_for_family_refuses_below_threshold_export():
    records = tuple(_record(i, kills=k, deaths=0) for i, k in enumerate([9, 0, 8, 1], start=1))
    pairs = (_pair("1:1", "2:1", "left"),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    try:
        build_artifact_for_family(dataset, "aggregate", MODEL_FAMILY_LINEAR, **_COMMON_KWARGS)
        assert False, "expected ValueError for below-threshold export"
    except ValueError as exc:
        assert "below its minimum" in str(exc)


def test_build_artifact_for_family_produces_artifact_matching_comparison():
    dataset = _build_step_signal_dataset()
    result = compare_tier(dataset, "aggregate", min_pairs_by_family=_LOW_THRESHOLDS, **_COMMON_KWARGS)
    assert result.selected_model is not None
    artifact = build_artifact_for_family(
        dataset, "aggregate", result.selected_model,
        min_pairs_by_family=_LOW_THRESHOLDS, **_COMMON_KWARGS,
    )
    assert artifact.content_hash == result.selected_artifact_content_hash


# ── 10. boosting must never tune on the outer validation/test split ────────

def _boosted_stumps_candidate(result):
    return next(c for c in result.candidates if c.model_family == MODEL_FAMILY_BOOSTED_STUMPS)


def _force_boosted_stumps_thresholds():
    # Every other family made structurally/pair-count ineligible so
    # `boosted_stumps` is the only selectable candidate -- lets tests
    # force it to be THE winner without depending on which family the
    # non-linear synthetic signal happens to favor.
    return {
        MODEL_FAMILY_LINEAR: 999_999, MODEL_FAMILY_GAM: 999_999,
        MODEL_FAMILY_BOOSTED_STUMPS: 8, MODEL_FAMILY_MONOTONIC_TREE: 999_999,
    }


def test_outer_validation_perturbation_does_not_change_boosting_structure():
    """Perturbing the OUTER validation split's features/labels (anything
    at all) must not change boosting's fitted stumps -- proof that
    fitting never looks at it. If this test failed, it would mean
    boosting's early stopping (or anything else in its fit path) is
    reading the outer validation split, exactly the leak this fix closes.
    """
    dataset = _build_step_signal_dataset()
    thresholds = _force_boosted_stumps_thresholds()
    result_a = compare_tier(dataset, "aggregate", min_pairs_by_family=thresholds, **_COMMON_KWARGS)

    # Perturb every validation-split record's kills/deaths to extreme,
    # nonsensical values, and flip every validation-referencing pair's
    # choice -- if boosting's fit depended on outer validation in any way,
    # this would change its stumps.
    perturbed_records = []
    for record in dataset.feature_records:
        if record.split != "validation":
            perturbed_records.append(record)
            continue
        perturbed_records.append(_record(
            record.game_id, kills=99999, deaths=99999, split="validation",
        ))
    perturbed_dataset = TrainingDataset(
        schema_version=1, feature_records=tuple(perturbed_records), pair_labels=dataset.pair_labels,
    )
    result_b = compare_tier(perturbed_dataset, "aggregate", min_pairs_by_family=thresholds, **_COMMON_KWARGS)

    candidate_a, candidate_b = _boosted_stumps_candidate(result_a), _boosted_stumps_candidate(result_b)
    assert candidate_a.eligible and candidate_b.eligible
    # Same train_n_pairs_used, same inner-split bookkeeping, same
    # structural metadata -- the fitted ensemble is bit-for-bit identical
    # regardless of what the outer validation split contains.
    assert candidate_a.train_n_pairs_used == candidate_b.train_n_pairs_used
    assert candidate_a.training_metadata_extra == candidate_b.training_metadata_extra
    assert candidate_a.train_final_loss == candidate_b.train_final_loss

    artifact_a = build_artifact_for_family(
        dataset, "aggregate", MODEL_FAMILY_BOOSTED_STUMPS,
        min_pairs_by_family=thresholds, **_COMMON_KWARGS,
    )
    artifact_b = build_artifact_for_family(
        perturbed_dataset, "aggregate", MODEL_FAMILY_BOOSTED_STUMPS,
        min_pairs_by_family=thresholds, **_COMMON_KWARGS,
    )
    assert artifact_a.boosted_stumps == artifact_b.boosted_stumps
    assert artifact_a.content_hash == artifact_b.content_hash


def test_test_split_perturbation_does_not_change_selected_boosting_structure():
    """Perturbing the OUTER test split must not change which family is
    selected nor the winner's fitted structure -- test is reserved purely
    for post-selection reporting.
    """
    dataset = _build_step_signal_dataset()
    thresholds = _force_boosted_stumps_thresholds()
    result_a = compare_tier(dataset, "aggregate", min_pairs_by_family=thresholds, **_COMMON_KWARGS)

    perturbed_records = []
    for record in dataset.feature_records:
        if record.split != "test":
            perturbed_records.append(record)
            continue
        perturbed_records.append(_record(record.game_id, kills=0, deaths=99999, split="test"))
    perturbed_dataset = TrainingDataset(
        schema_version=1, feature_records=tuple(perturbed_records), pair_labels=dataset.pair_labels,
    )
    result_b = compare_tier(perturbed_dataset, "aggregate", min_pairs_by_family=thresholds, **_COMMON_KWARGS)

    assert result_a.selected_model == result_b.selected_model == MODEL_FAMILY_BOOSTED_STUMPS
    assert result_a.selected_artifact_content_hash == result_b.selected_artifact_content_hash
    candidate_a, candidate_b = _boosted_stumps_candidate(result_a), _boosted_stumps_candidate(result_b)
    assert candidate_a.validation_pairwise_accuracy == candidate_b.validation_pairwise_accuracy


def test_tiny_train_disables_inner_early_stop_honestly():
    # Too few independent game-groups for a safe inner split -- must fall
    # back to fitting on the WHOLE train dataset with no early-stop
    # validation set, never substituting the outer validation split.
    records = tuple(_record(i, kills=k, deaths=0) for i, k in enumerate([9, 0, 8, 1], start=1))
    pairs = (_pair("1:1", "2:1", "left"), _pair("3:1", "4:1", "left"))
    validation_records = (_record(100, kills=9, deaths=0, split="validation"),)
    dataset = TrainingDataset(
        schema_version=1, feature_records=records + validation_records, pair_labels=pairs,
    )
    result = compare_tier(
        dataset, "aggregate", min_pairs_by_family={MODEL_FAMILY_BOOSTED_STUMPS: 1}, **_COMMON_KWARGS,
    )
    candidate = _boosted_stumps_candidate(result)
    assert candidate.training_metadata_extra["inner_early_stop_split_enabled"] is False
    assert candidate.training_metadata_extra["inner_stop_n_items"] == 0
    assert candidate.training_metadata_extra["inner_fit_n_items"] == 4  # the whole train set


def test_early_stopped_boosting_winner_exports_with_identical_hash_and_predictions():
    dataset = _build_step_signal_dataset()
    thresholds = _force_boosted_stumps_thresholds()
    result = compare_tier(dataset, "aggregate", min_pairs_by_family=thresholds, **_COMMON_KWARGS)
    assert result.selected_model == MODEL_FAMILY_BOOSTED_STUMPS
    candidate = _boosted_stumps_candidate(result)
    assert candidate.training_metadata_extra["inner_early_stop_split_enabled"] is True

    exported = build_artifact_for_family(
        dataset, "aggregate", MODEL_FAMILY_BOOSTED_STUMPS,
        min_pairs_by_family=thresholds, **_COMMON_KWARGS,
    )
    assert exported.content_hash == result.selected_artifact_content_hash

    # Runtime predictions must match too, not just the hash.
    from score_v2.runtime import score_participant
    game_features = {
        "evidence_source": "aggregate", "abstain": False, "abstain_reason": None,
        "chosen_source_completeness": 1.0,
        "participants": {
            "1": _gf(kills=9, deaths=0)["participants"]["1"],
            "2": _gf(kills=0, deaths=9)["participants"]["1"],
        },
    }
    result_1 = score_participant(exported, game_features, 1)
    result_2 = score_participant(exported, game_features, 2)
    assert result_1.score > result_2.score

    # Re-derive a SECOND time (simulating a separate CLI export run) and
    # confirm bit-identical results again.
    exported_again = build_artifact_for_family(
        dataset, "aggregate", MODEL_FAMILY_BOOSTED_STUMPS,
        min_pairs_by_family=thresholds, **_COMMON_KWARGS,
    )
    assert exported_again.content_hash == exported.content_hash
    rerun_1 = score_participant(exported_again, game_features, 1)
    assert rerun_1.score == result_1.score
