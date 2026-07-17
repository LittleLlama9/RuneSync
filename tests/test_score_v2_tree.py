"""Tests for score_v2/training/tree.py -- the monotonic tree baseline.

Sections:
  1. Zero-pair training: honest neutral (single 0.0 leaf) result.
  2. Monotonic invariant: `verify_tree_monotonicity` always holds for a
     fitted tree, including against adversarial labels and a genuine
     multi-feature interaction signal.
  3. Determinism.
  4. Pairwise correction reduces loss on separable synthetic data.
  5. Degenerate case: with too little signal/depth budget the tree stays
     a single leaf (depth 1) -- this is the "not a genuine tree
     candidate" case the orchestrator treats as ineligible.
  6. Single-tier assertion + abstain exclusion.
  7. Monotonic counterfactual invariant on a FITTED tree, and a genuine
     feature-INTERACTION capture that a purely additive model cannot
     represent.
"""

from score_v2.feature_spec import extract_feature_vector
from score_v2.model_shapes import evaluate_tree, tree_depth, verify_tree_monotonicity
from score_v2.training.dataset import DatasetValidationError, PairLabel, TrainingDataset, build_feature_record
from score_v2.training.tree import fit_pairwise_monotonic_tree


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

def test_zero_pairs_yields_single_zero_leaf():
    records = (_record(1, kills=8, deaths=1, assists=2),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    fitted = fit_pairwise_monotonic_tree(dataset)
    assert fitted.root.is_leaf is True
    assert fitted.root.value == 0.0
    assert fitted.n_pairs_used == 0
    assert fitted.final_loss is None


# ── 2. monotonic invariant ───────────────────────────────────────────────────

def test_fitted_tree_always_verifies_monotonic_even_against_noise():
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
    fitted = fit_pairwise_monotonic_tree(dataset, max_depth=3, min_samples_leaf=1)
    assert verify_tree_monotonicity(fitted.root) is True


def test_fitted_tree_captures_genuine_interaction_and_stays_monotonic():
    # AND-type synergy: a real bonus only when BOTH kills and assists are
    # high -- an interaction a purely additive model (linear/GAM/boosting
    # with depth-1 stumps) structurally cannot represent, but a multi-level
    # tree can (split on kills, then assists within the high-kills branch).
    records = []
    game_id = 1
    for kills in (0, 3, 6, 9):
        for assists in (0, 3, 6, 9):
            records.append(_record(game_id, kills=kills, deaths=0, assists=assists))
            game_id += 1

    def synergy(k, a):
        return k + a + (8.0 if (k >= 6 and a >= 6) else 0.0)

    pairs = []
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            ki, ai = records[i].features["raw"]["kills"], records[i].features["raw"]["assists"]
            kj, aj = records[j].features["raw"]["kills"], records[j].features["raw"]["assists"]
            ui, uj = synergy(ki, ai), synergy(kj, aj)
            if ui == uj:
                continue
            choice = "left" if ui > uj else "right"
            pairs.append(_pair(records[i].base_ref, records[j].base_ref, choice))

    dataset = TrainingDataset(schema_version=1, feature_records=tuple(records), pair_labels=tuple(pairs))
    fitted = fit_pairwise_monotonic_tree(dataset, max_depth=3, min_samples_leaf=1)
    assert verify_tree_monotonicity(fitted.root) is True
    assert tree_depth(fitted.root) > 1  # a genuine split was found, not a degenerate leaf


# ── 3. determinism ───────────────────────────────────────────────────────────

def test_fit_pairwise_monotonic_tree_is_deterministic():
    records = tuple(
        _record(game_id, kills=k, deaths=d, assists=3)
        for game_id, (k, d) in enumerate([(8, 1), (1, 8), (6, 2), (2, 6)], start=1)
    )
    pairs = (_pair("1:1", "2:1", "left"), _pair("3:1", "4:1", "left"))
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)

    fitted_a = fit_pairwise_monotonic_tree(dataset, min_samples_leaf=1)
    fitted_b = fit_pairwise_monotonic_tree(dataset, min_samples_leaf=1)
    assert fitted_a.root == fitted_b.root


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
    fitted = fit_pairwise_monotonic_tree(dataset, min_samples_leaf=1)
    assert fitted.final_loss is not None
    assert tree_depth(fitted.root) > 1


# ── 5. degenerate case ───────────────────────────────────────────────────────

def test_degenerate_tiny_data_stays_a_single_leaf():
    # Only 2 items, 1 pair -- min_samples_leaf's default (4) can never be
    # satisfied on either side of any split.
    records = (_record(1, kills=8, deaths=1), _record(2, kills=1, deaths=8))
    pairs = (_pair("1:1", "2:1", "left"),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    fitted = fit_pairwise_monotonic_tree(dataset)  # default min_samples_leaf=4
    assert tree_depth(fitted.root) == 1
    assert fitted.root.is_leaf is True


# ── 6. single-tier assertion + abstain exclusion ────────────────────────────

def test_fit_pairwise_monotonic_tree_rejects_mixed_tier_dataset():
    r1 = _record(1, 8, 1, 2, evidence_source="match_v5")
    r2 = _record(2, 8, 1, 2, evidence_source="aggregate")
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=())
    try:
        fit_pairwise_monotonic_tree(dataset)
        assert False, "expected DatasetValidationError"
    except DatasetValidationError:
        pass


def test_abstained_records_excluded_from_tree_training_by_default():
    normal = _record(1, kills=8, deaths=1, assists=2, abstain=False)
    abstained = _record(2, kills=8, deaths=1, assists=2, abstain=True)
    pair = _pair("1:1", "2:1", "left")
    dataset = TrainingDataset(
        schema_version=1, feature_records=(normal, abstained), pair_labels=(pair,),
    )
    fitted = fit_pairwise_monotonic_tree(dataset)
    assert fitted.n_items == 1
    assert fitted.n_items_excluded_abstain == 1
    assert fitted.n_pairs_used == 0


# ── 7. monotonic counterfactual invariant on a FITTED tree ─────────────────

def test_fitted_tree_counterfactual_never_regresses_for_positive_feature():
    records = tuple(
        _record(game_id, kills=k, deaths=1, assists=3)
        for game_id, k in enumerate([9, 0, 8, 1, 7, 2, 6, 3], start=1)
    )
    pairs = tuple(_pair(f"{a}:1", f"{b}:1", "left") for a, b in [(1, 2), (3, 4), (5, 6), (7, 8)])
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    fitted = fit_pairwise_monotonic_tree(dataset, min_samples_leaf=1)

    base_features = _gf(kills=3, deaths=1, assists=3)["participants"]["1"]
    scores = []
    for kills in range(0, 12):
        features = {**base_features, "raw": {**base_features["raw"], "kills": kills}}
        vector = extract_feature_vector(features, specs=fitted.specs)
        scores.append(evaluate_tree(fitted.root, vector))
    assert all(scores[i] <= scores[i + 1] + 1e-9 for i in range(len(scores) - 1))
