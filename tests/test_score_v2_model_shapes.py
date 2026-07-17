"""Tests for score_v2/model_shapes.py -- the shared runtime-layer shape
representations (`FeatureShapeFit`/`Stump`/`TreeNode`) and their
evaluation functions, used identically by training-time comparison
(`score_v2.training.gam`/`boosting`/`tree`) and shipped runtime scoring
(`score_v2.artifact`/`score_v2.runtime`).

Sections:
  1. FeatureShapeFit (GAM): piecewise-linear interpolation, flat
     extrapolation beyond the outer knots, missing-value -> 0.0,
     to_dict/from_dict round-trip.
  2. Stump (boosted weak learner): threshold routing, missing-value ->
     0.0, round-trip.
  3. TreeNode (monotonic tree): routing through multiple levels, missing
     split feature -> whole-tree 0.0, tree_depth/tree_node_count/
     tree_value_range, verify_tree_monotonicity (both directions, and
     detecting a deliberately-broken tree), collect_tree_feature_names/
     collect_tree_specs, round-trip.
"""

from score_v2.feature_spec import (
    DIRECTION_NEGATIVE,
    DIRECTION_POSITIVE,
    FeatureValue,
    feature_contract_for_tier,
)
from score_v2.model_shapes import (
    FeatureShapeFit,
    Stump,
    TreeNode,
    collect_tree_feature_names,
    collect_tree_specs,
    evaluate_boosted_stumps,
    evaluate_gam_shapes,
    evaluate_tree,
    tree_depth,
    tree_node_count,
    tree_value_range,
    verify_tree_monotonicity,
)

_SPECS = feature_contract_for_tier("aggregate")
KILLS_SPEC = next(s for s in _SPECS if s.name == "raw_kills")  # DIRECTION_POSITIVE
DEATHS_SPEC = next(s for s in _SPECS if s.name == "raw_deaths")  # DIRECTION_NEGATIVE


def _present(name, transformed):
    return FeatureValue(name=name, raw=transformed, transformed=transformed, present=True)


def _missing(name):
    return FeatureValue(name=name, raw=None, transformed=None, present=False)


# ── 1. FeatureShapeFit (GAM) ─────────────────────────────────────────────────

def test_feature_shape_fit_interpolates_between_knots():
    shape = FeatureShapeFit(
        spec=KILLS_SPEC, robust_center=0.0, robust_scale=1.0,
        knot_x=(-1.0, 0.0, 1.0), knot_y=(-2.0, 0.0, 2.0),
    )
    assert shape.evaluate(0.5) == 1.0  # halfway between knot 1 (0.0) and knot 2 (1.0, y=2.0)
    assert shape.evaluate(0.0) == 0.0
    assert shape.evaluate(-1.0) == -2.0


def test_feature_shape_fit_flat_extrapolates_beyond_outer_knots():
    shape = FeatureShapeFit(
        spec=KILLS_SPEC, robust_center=0.0, robust_scale=1.0,
        knot_x=(-1.0, 0.0, 1.0), knot_y=(-2.0, 0.0, 2.0),
    )
    assert shape.evaluate(5.0) == 2.0  # clamped to the rightmost knot's y
    assert shape.evaluate(-5.0) == -2.0


def test_feature_shape_fit_missing_value_is_zero():
    shape = FeatureShapeFit(
        spec=KILLS_SPEC, robust_center=0.0, robust_scale=1.0,
        knot_x=(-1.0, 1.0), knot_y=(5.0, 9.0),  # even a shape with no zero-crossing
    )
    assert shape.evaluate(None) == 0.0


def test_feature_shape_fit_round_trip():
    shape = FeatureShapeFit(
        spec=KILLS_SPEC, robust_center=1.5, robust_scale=2.5,
        knot_x=(-1.0, 0.0, 1.0), knot_y=(-2.0, 0.0, 2.0),
    )
    restored = FeatureShapeFit.from_dict(shape.to_dict())
    assert restored == shape


def test_evaluate_gam_shapes_sums_present_features_and_skips_missing():
    kills_shape = FeatureShapeFit(
        spec=KILLS_SPEC, robust_center=0.0, robust_scale=1.0,
        knot_x=(-1.0, 1.0), knot_y=(-3.0, 3.0),
    )
    deaths_shape = FeatureShapeFit(
        spec=DEATHS_SPEC, robust_center=0.0, robust_scale=1.0,
        knot_x=(-1.0, 1.0), knot_y=(3.0, -3.0),
    )
    vector = {"raw_kills": _present("raw_kills", 0.0), "raw_deaths": _missing("raw_deaths")}
    assert evaluate_gam_shapes([kills_shape, deaths_shape], vector) == 0.0  # only kills at its midpoint (y=0)

    vector2 = {"raw_kills": _present("raw_kills", 1.0), "raw_deaths": _present("raw_deaths", 1.0)}
    assert evaluate_gam_shapes([kills_shape, deaths_shape], vector2) == 3.0 + (-3.0)


# ── 2. Stump (boosted weak learner) ─────────────────────────────────────────

def test_stump_routes_by_threshold():
    stump = Stump(
        spec=KILLS_SPEC, robust_center=0.0, robust_scale=1.0,
        threshold=0.0, low_value=-1.0, high_value=1.0,
    )
    assert stump.evaluate(-0.5) == -1.0
    assert stump.evaluate(0.0) == -1.0  # threshold itself routes low (<=)
    assert stump.evaluate(0.5) == 1.0


def test_stump_missing_value_is_zero():
    stump = Stump(
        spec=KILLS_SPEC, robust_center=0.0, robust_scale=1.0,
        threshold=0.0, low_value=-1.0, high_value=1.0,
    )
    assert stump.evaluate(None) == 0.0


def test_stump_round_trip():
    stump = Stump(
        spec=DEATHS_SPEC, robust_center=2.0, robust_scale=3.0,
        threshold=0.5, low_value=1.0, high_value=-1.0,
    )
    assert Stump.from_dict(stump.to_dict()) == stump


def test_evaluate_boosted_stumps_sums_and_skips_missing():
    stump_a = Stump(spec=KILLS_SPEC, robust_center=0.0, robust_scale=1.0, threshold=0.0, low_value=-1.0, high_value=1.0)
    stump_b = Stump(spec=KILLS_SPEC, robust_center=0.0, robust_scale=1.0, threshold=0.0, low_value=-2.0, high_value=2.0)
    vector = {"raw_kills": _present("raw_kills", 1.0)}
    assert evaluate_boosted_stumps([stump_a, stump_b], vector) == 3.0
    assert evaluate_boosted_stumps([stump_a, stump_b], {"raw_kills": _missing("raw_kills")}) == 0.0


# ── 3. TreeNode (monotonic tree) ─────────────────────────────────────────────

def _two_level_tree():
    """kills split at 0.0 (positive direction); the high branch further
    splits on deaths at 0.0 (negative direction). Value range: leaves are
    -2.0 (low kills), and within high-kills: 3.0 (low deaths, good) or
    1.0 (high deaths, still positive but worse) -- monotonic in both.
    """
    leaf_low_kills = TreeNode(is_leaf=True, value=-2.0)
    leaf_high_kills_low_deaths = TreeNode(is_leaf=True, value=3.0)
    leaf_high_kills_high_deaths = TreeNode(is_leaf=True, value=1.0)
    deaths_split = TreeNode(
        is_leaf=False, spec=DEATHS_SPEC, robust_center=0.0, robust_scale=1.0, threshold=0.0,
        low=leaf_high_kills_low_deaths, high=leaf_high_kills_high_deaths,
    )
    root = TreeNode(
        is_leaf=False, spec=KILLS_SPEC, robust_center=0.0, robust_scale=1.0, threshold=0.0,
        low=leaf_low_kills, high=deaths_split,
    )
    return root


def test_tree_node_routes_through_multiple_levels():
    root = _two_level_tree()
    low_kills_vector = {"raw_kills": _present("raw_kills", -1.0), "raw_deaths": _present("raw_deaths", -1.0)}
    assert evaluate_tree(root, low_kills_vector) == -2.0

    high_kills_low_deaths = {"raw_kills": _present("raw_kills", 1.0), "raw_deaths": _present("raw_deaths", -1.0)}
    assert evaluate_tree(root, high_kills_low_deaths) == 3.0

    high_kills_high_deaths = {"raw_kills": _present("raw_kills", 1.0), "raw_deaths": _present("raw_deaths", 1.0)}
    assert evaluate_tree(root, high_kills_high_deaths) == 1.0


def test_tree_node_missing_split_feature_yields_whole_tree_zero():
    root = _two_level_tree()
    # Missing the ROOT's own split feature -- cannot route at all.
    vector = {"raw_kills": _missing("raw_kills"), "raw_deaths": _present("raw_deaths", -1.0)}
    assert evaluate_tree(root, vector) == 0.0
    # Missing a DEEPER split feature after successfully routing once.
    vector2 = {"raw_kills": _present("raw_kills", 1.0), "raw_deaths": _missing("raw_deaths")}
    assert evaluate_tree(root, vector2) == 0.0


def test_tree_structural_helpers():
    root = _two_level_tree()
    assert tree_depth(root) == 3
    assert tree_node_count(root) == 5  # root + deaths_split + 3 leaves
    assert tree_value_range(root) == (-2.0, 3.0)


def test_tree_leaf_only_has_depth_one():
    leaf = TreeNode(is_leaf=True, value=0.0)
    assert tree_depth(leaf) == 1
    assert tree_node_count(leaf) == 1


def test_verify_tree_monotonicity_true_for_well_formed_tree():
    assert verify_tree_monotonicity(_two_level_tree()) is True


def test_verify_tree_monotonicity_detects_broken_positive_direction():
    # kills is DIRECTION_POSITIVE -- the "low kills" branch must never
    # exceed the "high kills" branch's value range. Deliberately swap them.
    leaf_low_kills = TreeNode(is_leaf=True, value=5.0)  # too high for the low branch
    leaf_high_kills = TreeNode(is_leaf=True, value=-5.0)  # too low for the high branch
    broken_root = TreeNode(
        is_leaf=False, spec=KILLS_SPEC, robust_center=0.0, robust_scale=1.0, threshold=0.0,
        low=leaf_low_kills, high=leaf_high_kills,
    )
    assert verify_tree_monotonicity(broken_root) is False


def test_verify_tree_monotonicity_detects_broken_negative_direction():
    # deaths is DIRECTION_NEGATIVE -- the "low deaths" branch (better)
    # must never be BELOW the "high deaths" branch (worse). Swap them.
    leaf_low_deaths = TreeNode(is_leaf=True, value=-5.0)
    leaf_high_deaths = TreeNode(is_leaf=True, value=5.0)
    broken_root = TreeNode(
        is_leaf=False, spec=DEATHS_SPEC, robust_center=0.0, robust_scale=1.0, threshold=0.0,
        low=leaf_low_deaths, high=leaf_high_deaths,
    )
    assert verify_tree_monotonicity(broken_root) is False


def test_collect_tree_feature_names_and_specs():
    root = _two_level_tree()
    assert collect_tree_feature_names(root) == {"raw_kills", "raw_deaths"}
    specs = collect_tree_specs(root)
    assert specs == {"raw_kills": KILLS_SPEC, "raw_deaths": DEATHS_SPEC}


def test_collect_tree_feature_names_empty_for_a_leaf():
    assert collect_tree_feature_names(TreeNode(is_leaf=True, value=0.0)) == set()
    assert collect_tree_specs(TreeNode(is_leaf=True, value=0.0)) == {}


def test_tree_node_round_trip():
    root = _two_level_tree()
    restored = TreeNode.from_dict(root.to_dict())
    assert restored == root
