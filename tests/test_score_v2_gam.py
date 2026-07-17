"""Tests for score_v2/training/gam.py -- the monotonic GAM baseline.

Sections:
  1. Zero-pair training: honest neutral (all-zero knot_y) shapes, not
     converged.
  2. Monotonic invariant: fitted knot_y sequences always respect each
     feature's declared direction, even against adversarial/noisy labels.
  3. Determinism (same input -> bit-identical output).
  4. Pairwise correction: training on separable synthetic data reduces
     loss and learns the domain-correct shape direction.
  5. Honest convergence: reflects a real loss-delta/gradient-norm
     stopping criterion, never "at least one pair existed".
  6. Single-tier assertion + abstained-record exclusion (inherited from
     score_v2.training.monotonic_utils, re-verified at this call site).
  7. Monotonic counterfactual invariant on a FITTED model: increasing a
     positive-direction feature (holding others fixed) never decreases
     evaluate_gam_shapes' output.
"""

from score_v2.feature_spec import DIRECTION_NEGATIVE, DIRECTION_POSITIVE, FEATURE_ALLOWLIST, extract_feature_vector
from score_v2.model_shapes import evaluate_gam_shapes
from score_v2.training.dataset import DatasetValidationError, PairLabel, TrainingDataset, build_feature_record
from score_v2.training.gam import fit_pairwise_gam


def _gf(kills, deaths, assists, abstain=False):
    return {
        "duration_seconds": 1800.0, "abstain": abstain, "abstain_reason": None,
        "chosen_source_completeness": 1.0,
        "participants": {"1": {
            "raw": {"kills": kills, "deaths": deaths, "assists": assists},
            "fight_influence": {
                "kill_events": kills, "death_events": deaths, "assist_events": assists,
                "first_blood": kills >= 6, "untraded_deaths": max(0, deaths - 2),
                "event_kill_participation": min(1.0, (kills + assists) / 10.0),
            },
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

def test_zero_pairs_yields_all_zero_shapes_and_not_converged():
    records = (_record(1, kills=8, deaths=1, assists=2),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    fitted = fit_pairwise_gam(dataset)
    assert all(
        all(y == 0.0 for y in shape.knot_y) for shape in fitted.shapes.values()
    )
    assert fitted.converged is False
    assert fitted.n_pairs_used == 0
    assert fitted.final_loss is None


# ── 2. monotonic invariant ───────────────────────────────────────────────────

def test_fitted_shapes_always_respect_declared_direction_even_against_noise():
    records = tuple(
        _record(game_id, kills=k, deaths=d, assists=3)
        for game_id, (k, d) in enumerate([
            (8, 1), (1, 8), (7, 2), (2, 7), (6, 3), (3, 6), (5, 4), (4, 5),
        ], start=1)
    )
    # Adversarial/noisy labels: always prefer the SECOND (lower-kill,
    # higher-death) item -- a naive unconstrained fit would want a
    # wrong-signed shape.
    pairs = tuple(
        _pair(f"{a}:1", f"{b}:1", "right") for a, b in zip(range(1, 9), range(2, 9))
    )
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    fitted = fit_pairwise_gam(dataset, iterations=150)
    for spec in FEATURE_ALLOWLIST:
        knot_y = fitted.shapes[spec.name].knot_y
        if spec.direction == DIRECTION_POSITIVE:
            assert all(knot_y[i] <= knot_y[i + 1] + 1e-9 for i in range(len(knot_y) - 1)), spec.name
        elif spec.direction == DIRECTION_NEGATIVE:
            assert all(knot_y[i] >= knot_y[i + 1] - 1e-9 for i in range(len(knot_y) - 1)), spec.name


# ── 3. determinism ───────────────────────────────────────────────────────────

def test_fit_pairwise_gam_is_deterministic():
    records = tuple(
        _record(game_id, kills=k, deaths=d, assists=3)
        for game_id, (k, d) in enumerate([(8, 1), (1, 8), (6, 2), (2, 6)], start=1)
    )
    pairs = (_pair("1:1", "2:1", "left"), _pair("3:1", "4:1", "left"))
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)

    fitted_a = fit_pairwise_gam(dataset, iterations=40)
    fitted_b = fit_pairwise_gam(dataset, iterations=40)
    for name in fitted_a.shapes:
        assert fitted_a.shapes[name].knot_y == fitted_b.shapes[name].knot_y
        assert fitted_a.shapes[name].knot_x == fitted_b.shapes[name].knot_x


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

    untrained = fit_pairwise_gam(dataset, iterations=0)
    trained = fit_pairwise_gam(dataset, iterations=200)

    assert untrained.final_loss is None or untrained.final_loss >= 0.693
    assert trained.final_loss is not None
    assert trained.final_loss < 0.4

    kills_shape = trained.shapes["raw_kills"]
    deaths_shape = trained.shapes["raw_deaths"]
    assert kills_shape.knot_y[-1] > kills_shape.knot_y[0]
    assert deaths_shape.knot_y[-1] < deaths_shape.knot_y[0]


# ── 5. honest convergence ───────────────────────────────────────────────────

def test_converged_true_only_when_a_real_tolerance_is_met():
    records = (
        _record(1, kills=9, deaths=0, assists=0), _record(2, kills=0, deaths=9, assists=0),
    )
    pairs = (_pair("1:1", "2:1", "left"),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    fitted = fit_pairwise_gam(
        dataset, iterations=5000, learning_rate=0.2, loss_tolerance=1e-6, gradient_tolerance=1e-5,
    )
    assert fitted.converged is True
    assert fitted.iterations_run < 5000


def test_converged_false_when_iteration_budget_exhausted_without_tolerance():
    records = (
        _record(1, kills=9, deaths=0, assists=0), _record(2, kills=0, deaths=9, assists=0),
    )
    pairs = (_pair("1:1", "2:1", "left"),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    fitted = fit_pairwise_gam(
        dataset, iterations=1, learning_rate=1e-6, loss_tolerance=1e-12, gradient_tolerance=1e-12,
    )
    assert fitted.converged is False
    assert fitted.iterations_run == 1


def test_converged_false_with_zero_usable_pairs_even_with_large_budget():
    records = (_record(1, kills=8, deaths=1, assists=2),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    fitted = fit_pairwise_gam(dataset, iterations=1000)
    assert fitted.converged is False
    assert fitted.iterations_run == 0


# ── 6. single-tier assertion + abstain exclusion ────────────────────────────

def test_fit_pairwise_gam_rejects_mixed_tier_dataset():
    r1 = _record(1, 8, 1, 2, evidence_source="match_v5")
    r2 = _record(2, 8, 1, 2, evidence_source="aggregate")
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=())
    try:
        fit_pairwise_gam(dataset)
        assert False, "expected DatasetValidationError"
    except DatasetValidationError:
        pass


def test_abstained_records_excluded_from_gam_training_by_default():
    normal = _record(1, kills=8, deaths=1, assists=2, abstain=False)
    abstained = _record(2, kills=8, deaths=1, assists=2, abstain=True)
    pair = _pair("1:1", "2:1", "left")
    dataset = TrainingDataset(
        schema_version=1, feature_records=(normal, abstained), pair_labels=(pair,),
    )
    fitted = fit_pairwise_gam(dataset)
    assert fitted.n_items == 1
    assert fitted.n_items_excluded_abstain == 1
    assert fitted.n_pairs_used == 0


# ── 7. monotonic counterfactual invariant on a FITTED model ─────────────────

def test_fitted_gam_counterfactual_never_regresses_for_positive_feature():
    records = tuple(
        _record(game_id, kills=k, deaths=1, assists=3)
        for game_id, k in enumerate([9, 0, 8, 1, 7, 2, 6, 3], start=1)
    )
    pairs = tuple(_pair(f"{a}:1", f"{b}:1", "left") for a, b in [(1, 2), (3, 4), (5, 6), (7, 8)])
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    fitted = fit_pairwise_gam(dataset, iterations=150)
    shapes = list(fitted.shapes.values())

    base_features = _gf(kills=3, deaths=1, assists=3)["participants"]["1"]
    scores = []
    for kills in range(0, 12):
        features = {**base_features, "raw": {**base_features["raw"], "kills": kills}}
        vector = extract_feature_vector(features, specs=fitted.specs)
        scores.append(evaluate_gam_shapes(shapes, vector))
    assert all(scores[i] <= scores[i + 1] + 1e-9 for i in range(len(scores) - 1))
