# DAEMON Score v2 Model Training/Evaluation/Runtime Pipeline

This document covers `score_v2/` (runtime + training package) and
`scripts/score_v2/*.py` (CLIs). It is the next stage after
`score_features.py` (evidence extraction, see the vault capability note
"Implemented `score_features.py`...") and `corpus/` (manifest, splits,
blinded pairwise review, see `docs/CORPUS_AND_REVIEW.md`). This package
does not touch feature extraction, corpus tooling, routing, UI, or
coaching -- those are owned separately.

**Status: pipeline-ready, not production-trained.** Every artifact this
pipeline can currently produce from the real local corpus is
`production_ready=False` with `training_metadata.status="insufficient_data"`,
because bulk Match-V5 acquisition and blinded human review are both still
blocked/tiny (see the vault decision "Gate final Score v2 validation on
Match-V5 authorization, not local feature development"). See
`docs/SCORE_V2_MODEL_CARD_TEMPLATE.md` for the release gates that must
pass before that changes.

## Scope and honesty principles

- **No outcome leakage, recursively.** `score_v2/leakage.py` scans every
  feature payload for outcome-shaped keys (`win`, `local_win`, `result`,
  `nexus`, `game_end`, `victory`, `defeat`, `surrender`, `remake`,
  `outcome`, and their camelCase/snake_case variants) using
  word-boundary tokenization -- NOT a raw substring match, because a raw
  substring match on `"win"` would also flag `score_features.py`'s own
  legitimate `resource_conversion.lead_windows`/`converted_lead_windows`
  fields (substring of "windows"). This is defense-in-depth on top of
  `score_v2/feature_spec.py`'s hand-reviewed allowlist, which is already
  leak-free by construction. Game outcome may only ever exist as a
  separate `score_v2.training.dataset.StateValueLabel` (auxiliary,
  offline-only, never merged into a `FeatureRecord`).
- **Runtime is dependency-free.** `score_v2/leakage.py`,
  `score_v2/feature_spec.py`, `score_v2/artifact.py`, and
  `score_v2/runtime.py` import only the Python standard library. A
  packaged RuneSync build can load and evaluate a Score v2 artifact with
  nothing beyond the interpreter.
- **Training is stdlib-only too, on purpose.** `score_v2/training/*.py`
  could use `numpy`/`scipy`/`sklearn` behind an optional import, but
  `requirements.txt` has none of those today, and with a corpus this
  small a hand-rolled deterministic linear/pairwise trainer is both
  sufficient and exactly reproducible in tests. See "Why a linear
  baseline" below.
- **Four separate evidence-tier artifacts, never silently substituted.**
  `score_v2.runtime.select_artifact` is an exact-key lookup by
  `evidence_source` (`match_v5`, `lcu_timeline`, `live_client`,
  `aggregate`) with no implicit fallback. If one tier's coefficients were
  actually fit from/shrunk toward another tier's data, that is recorded
  as explicit `fallback` metadata *on the artifact itself*
  (`{"is_fallback": true, "shrinkage_source": "..."}`) at training time --
  never invented at routing time.
- **Nothing here is a production artifact.**
  `score_v2.training.export.train_tier` always sets
  `production_ready=False`; nothing in this package can set it `True`.

## Package layout

```
score_v2/
  leakage.py       -- recursive outcome-key scanner (runtime + training)
  feature_spec.py  -- the canonical, hand-reviewed feature allowlist
  artifact.py       -- immutable, SHA-256-hashed artifact format (all 4 model families)
  model_shapes.py   -- shared GAM/boosted-stump/tree shape dataclasses + evaluation
  runtime.py        -- dependency-free scorer + tier routing (dispatches by model_family)
  training/
    dataset.py         -- FeatureRecord / PairLabel / StateValueLabel schema
    monotonic_utils.py -- shared pairwise-fit prep (normalization, PAVA, leakage-safe eval prep)
    baseline.py         -- regularized pairwise LINEAR trainer
    gam.py              -- monotonic GAM trainer (piecewise-linear per-feature shapes)
    boosting.py         -- monotonic additive BOOSTING trainer (shallow stumps)
    tree.py             -- monotonic TREE trainer (single depth-bounded CART)
    calibration.py      -- role offsets/shrinkage + score mapping (linear + generic score_fn)
    evaluate.py           -- grouped evaluation metrics
    export.py             -- ties baseline+calibration into a saved linear Artifact per tier
    compare.py             -- 4-family comparison orchestrator (train/validate/test discipline)
scripts/score_v2/
  build_training_dataset.py -- HistoryStore + corpus manifest -> dataset.jsonl
  train_model.py              -- dataset.jsonl -> artifacts/<tier>.json (linear only)
  evaluate_model.py            -- dataset.jsonl + artifacts -> report.json
  compare_models.py             -- dataset.jsonl -> per-tier 4-family comparison report.json
```

## The feature contract (`score_v2/feature_spec.py`)

24 hand-reviewed `FeatureSpec`s, each a dotted `path` into one
participant's `score_features.compute_feature_set(...)` block, a
monotonic `direction` (+1 higher-is-better, -1 lower-is-better, 0
unconstrained), a `transform` (`identity`, `log1p` for counts, `clamp01`
for rates already in [0,1]), and a `required_capability` label
(`always`, `event_evidence`, `ward_events`, `minute_frames`,
`live_snapshots` -- informative only; real presence is always determined
dynamically by walking `path`, since `score_features.py` already encodes
honest per-participant availability). `required_capability` also defines
each evidence tier's canonical, immutable **feature contract**
(`TIER_FEATURE_CONTRACTS`/`feature_contract_for_tier`) -- see below.

**Deliberately excluded raw stats**: `raw.vision_score`,
`raw.wards_placed`, `raw.wards_killed`, `raw.damage_to_turrets`,
`raw.damage_to_objectives`, `raw.damage_to_champions`, `raw.gold_earned`,
and `raw.cs`. These remain in `score_features.py`'s `raw` block for
provenance but are never model inputs -- feeding raw vision/damage in
directly would reproduce the exact DAEMON v1 regression this project
exists to fix (Seraphine's raw vision score inflating her v1 score with
zero actionable map impact; Vel'Koz's turret damage share being credited
as objective influence with no real secure/assist). Raw gold/CS are
excluded for the same reason -- neither is causally validated influence
on its own; only `resource_conversion_rate` (a causally-filtered
gold-LEAD conversion signal) represents economy influence here.

**Deliberately excluded objective-assist fields**:
`objective_participation.epic_monster_assists` and `.grub_assists`.
Riot's raw monster-kill "assist" credit for these events can be awarded
on loose proximity/tick criteria, not a verified fight contribution, so
it is not used as monotonic influence. `objective_fight_involvements` (a
spatially/temporally causal-filtered signal `score_features.py` already
computes) is used instead.

### Per-tier feature contracts

The four evidence tiers do not all support the same features -- rather
than one universal list where a weaker tier permanently carries
always-absent features, each tier gets an explicit contract built from
only the specs whose `required_capability` that tier actually supports:

| Tier | Capabilities | Feature count | Notably excludes |
|---|---|---|---|
| `match_v5` | always, event_evidence, ward_events, minute_frames | 23 | `live_dead_sample_rate` (no live snapshots) |
| `lcu_timeline` | always, event_evidence, minute_frames | 22 | `vision_actionable_rate` (verified no ward events), `live_dead_sample_rate` |
| `live_client` | always, event_evidence, live_snapshots | 22 | `vision_actionable_rate`, `resource_conversion_rate` (no minute frames) |
| `aggregate` | always | **3** | everything except `raw_kills`/`raw_deaths`/`raw_assists` |

`aggregate`'s contract is intentionally just the three always-available
raw KDA counts -- it makes **no claim of objective, vision, or
economy-conversion evidence at all**. `score_v2.artifact.Artifact.validate()`
requires a loaded artifact's coefficients to match its own tier's
contract **exactly** (same names, same path/direction/transform/
capability/group) -- an artifact with an arbitrary raw path, an extra or
missing feature, or a tampered-but-same-named spec is rejected even if
its `content_hash` was recomputed to match ("rehashed").

## Why a linear baseline

With the current corpus (a handful of local matches, zero real blinded
pairwise labels as of this stage), any model with more capacity than a
regularized linear function would overfit invisibly -- no evaluation
metric in `score_v2.training.evaluate` could tell real signal from noise
at this sample size. `score_v2.training.baseline.fit_pairwise_baseline`
fits

    raw_linear(x) = sum_i coefficient_i * normalized(x_i)

by full-batch gradient descent on a confidence-weighted pairwise logistic
(Bradley-Terry) loss, with L2 regularization and a **monotonic sign
projection** after every gradient step: a coefficient whose `FeatureSpec`
says "higher is better" is clipped back to `>= 0`, and vice versa, so a
tiny or noisy corpus can never flip "more kills" into a score penalty.
Robust (median/MAD) normalization is fit once from the training rows.
Zero usable pairwise labels (today's real state) yields exactly the
zero/neutral L2 prior -- every coefficient stays 0, which is the honest
answer, not a fabricated one.

**There is deliberately no trained intercept.** In a pairwise comparison
`diff = s(left) - s(right) = sum_i coefficient_i * (left_i - right_i)`, a
shared additive intercept cancels out exactly regardless of its value --
it is mathematically unidentifiable from pairwise-only supervision. An
earlier version of this trainer computed a "gradient" for the intercept
anyway; that gradient was phantom (not a real derivative of the loss,
since the intercept has zero effect on `diff`). The intercept now stays
fixed at `0.0` always; centering is the job of `score_v2.training.calibration`'s
role/score calibration layers, which do have a principled way to set an
offset.

**`converged` is a real stopping-criterion flag**, not "at least one pair
existed": training tracks the loss delta and gradient norm each
iteration and only reports `converged=True` if one drops below its
tolerance (`loss_tolerance`/`gradient_tolerance`) before the iteration
budget is exhausted. Running out of iterations without meeting either
tolerance is honestly `converged=False`.

**Insufficient data produces a genuinely neutral artifact, not a masked
real fit.** If `n_pairs_used < min_pairs_for_nontrivial_fit` (default
20), `score_v2.training.export.train_tier` discards whatever the
underlying fit computed entirely and exports: every coefficient `0.0`,
every normalization a no-op (`center=0.0, scale=1.0`), every role offset
`0.0`, and the fixed default score scale -- not merely a status label
slapped on a statistically-unreliable-but-real fit. `training_metadata`
still reports the true `n_pairs_used`/`n_pairs_skipped`/`n_items`
honestly either way. A real ("exploratory") nonzero fit is only ever
exported when a caller **explicitly lowers** `min_pairs_for_nontrivial_fit`
below the tier's actual usable-pair count (e.g. for a documented research
run) -- never silently.

Richer model classes (a monotonic GAM, monotonic additive boosting, and a
monotonic tree) now exist alongside this linear baseline and are compared
against it -- see "Model family comparison" below. **The comparison/
evaluation/export pipeline itself is complete**; what remains gated is
*production calibration*, which still requires a real corpus (bulk
Match-V5 acquisition + blinded human review), not more code. With today's
real, near-zero-pairwise-label corpus, every family is honestly
`ineligible` and the comparison's `selected_model` is `null` -- this is
the expected, correct output at this data scale, not a bug.

**Abstained records are excluded from training and calibration by
default.** A `FeatureRecord` with `abstain=True` (e.g. `score_features.py`'s
own short-game flag) is excluded from robust-normalization fitting,
pairwise training, and role/score calibration in
`fit_pairwise_baseline`/`fit_role_calibration`/`fit_score_calibration` --
its feature values are exactly the kind of noise the `abstain` flag warns
about. `include_abstained=True` overrides this explicitly. Every other
model family (GAM/boosting/tree) follows the identical exclusion rule via
the shared `score_v2.training.monotonic_utils.prepare_pairwise_data`.

## Model family comparison (`score_v2/training/compare.py`)

Four monotonic model families are fit and compared, per evidence tier:

| Family | Module | Model form | Capacity |
|---|---|---|---|
| `linear` | `training/baseline.py` | `sum_i coefficient_i * normalized(x_i)` | 1 parameter/feature |
| `gam` | `training/gam.py` | `sum_i shape_i(x_i)`, each `shape_i` a small piecewise-linear curve | ~5 knots/feature |
| `boosted_stumps` | `training/boosting.py` | `sum_t shrinkage * stump_t(x)`, many shallow additive weak learners | grows with rounds |
| `monotonic_tree` | `training/tree.py` | one depth-bounded CART-style tree, captures feature INTERACTIONS | `2^depth - 1` splits |

All four share the exact same per-tier feature contract, multi-tier
`base_ref` semantics, abstain exclusion, `PairLabel` validation, and
Bradley-Terry pairwise loss (via `training/monotonic_utils.py`) -- so a
comparison across families is apples-to-apples (same items, same
normalization statistics, same usable pairs). Monotonicity is enforced
for every family, not just asserted after the fact:

- **linear**: a scalar sign clip on the coefficient (pre-existing).
- **GAM**: `training/monotonic_utils.isotonic_projection` (PAVA -- the
  true L2-optimal monotonic projection) applied to each feature's knot
  y-values after every gradient step.
- **boosting**: PAVA applied to each stump's 2-element
  `(low_value, high_value)` after every round.
- **tree**: **value-range interval propagation** (the same technique used
  by XGBoost/LightGBM's `monotone_constraints`) -- every node inherits an
  allowed prediction interval from its parent, and a split narrows the
  child intervals in the direction-consistent way, so the WHOLE TREE is
  provably monotonic regardless of which features it splits on anywhere
  in it. `score_v2.model_shapes.verify_tree_monotonicity` independently
  re-verifies this bottom-up (does not trust the training code), and
  `score_v2.artifact.Artifact.validate()` calls it on every loaded
  `monotonic_tree` artifact.

**`score_v2/model_shapes.py`** is a new, shared, stdlib-only RUNTIME-layer
module holding the `FeatureShapeFit`/`Stump`/`TreeNode` dataclasses (each
with `to_dict()`/`from_dict()`) and their `evaluate_*` functions.
`score_v2.artifact`/`score_v2.runtime` (runtime layer) and
`training/gam.py`/`boosting.py`/`tree.py` (training layer) both depend on
it -- never the reverse -- so there is exactly one implementation of each
shape's math, used identically whether scoring a fitted-in-memory
candidate during comparison or a saved, hash-verified `Artifact` at
shipped runtime.

**Artifact/runtime extension.** `Artifact` gained `model_family` (one of
`linear`/`gam`/`boosted_stumps`/`monotonic_tree`) plus three optional
shape fields (`gam_shapes`/`boosted_stumps`/`monotonic_tree`, exactly one
populated for a non-linear family). `Artifact.validate()` dispatches per
family: the linear family still requires EXACT full coverage of its
tier's canonical contract (as before); the three non-linear families may
legitimately use only a SUBSET of it, but every feature spec they DO use
must exactly match the canonical contract (name/path/direction/
transform/capability/group), all knot/threshold/value parameters must be
finite, and each family's monotonic order is independently re-verified
(never just trusted from training). `score_v2.runtime.score_participant`
dispatches raw-score computation by `artifact.model_family`; role/score
calibration, confidence, abstention, and tier-mismatch enforcement are
completely unchanged regardless of family. `total_feature_count`/
`missing_features` reflect only the features THAT family actually uses
-- a GAM using one of aggregate's three features is not penalized for
"missing" the other two, which it never claimed to need.

**Calibration** gained generic, family-agnostic siblings
(`fit_role_calibration_for_score_fn`/`fit_score_calibration_for_score_fn`,
taking a plain `score_fn(record) -> float` instead of a linear-specific
`FittedBaseline`) so every non-linear candidate can be role/score
calibrated identically to the linear family; the original linear-specific
functions are now thin, behavior-preserving wrappers around these.

**Eligibility gating (`compare_tier`).** Each family is fit on the TRAIN
split unconditionally (for honest metadata/parameter-count reporting),
but is only considered `eligible` for validation-based comparison if its
`n_pairs_used` clears a per-family minimum -- deliberately conservative,
materially higher multiples of the linear baseline's own long-established
`MIN_PAIRS_FOR_NONTRIVIAL_FIT` (20), reflecting each family's higher
capacity/overfitting risk:

```
MIN_PAIRS_LINEAR = 20            # unchanged from the linear-only stage
MIN_PAIRS_GAM = 80
MIN_PAIRS_BOOSTED_STUMPS = 120
MIN_PAIRS_MONOTONIC_TREE = 60
```

**These thresholds are honest judgment calls, not empirically validated**
-- there is no real corpus at any meaningful scale to validate them
against yet (see the vault decision gating Score v2 on the blocked
Match-V5 authorization). A degenerate tree fit (no split survives its own
`min_samples_leaf`/`min_gain` guardrails, i.e. `tree_depth(root) <= 1`) is
ALSO marked ineligible regardless of pair count -- a lone leaf is never
presented as a genuine, architecturally-distinct tree candidate.

**Selection is validation-only.** For every eligible candidate, one
complete, self-consistent, hashed `Artifact` is built in memory (never
saved to disk by `compare_tier` itself) and evaluated on the VALIDATION
split via the real `score_v2.runtime.score_participant` path and the full
`score_v2.training.evaluate.evaluate_dataset` suite. The family with the
highest VALIDATION pairwise accuracy wins -- **train metrics never
influence selection**. Ties are broken deterministically: fewer
`n_parameters` first (prefer the simpler model), then family name. If no
eligible candidate produced a scoreable validation pairwise accuracy,
`selected_model=None` -- there is no fallback winner. Only AFTER
selection is the winning artifact evaluated on the TEST split, purely for
reporting; test evaluation never influences which family was chosen.

**No validation/test leakage.** Every candidate's normalization
statistics (`robust_center`/`robust_scale`) are fit from the TRAIN split
alone via `prepare_pairwise_data`; validation/test scoring goes through
`prepare_pairwise_eval_data`/the built `Artifact`'s stored (train-fit)
normalization -- held-out data can never influence what "typical"/"scale"
means for any feature. **This is enforced for boosting's own internal
early-stopping mechanism too** (see "Boosting's inner early-stop split"
below) -- the OUTER validation split stays completely unseen by every
family, including boosting, until the cross-family selection step above.

**Empirically verified, not merely claimed:** with a genuinely non-linear
monotonic synthetic signal (a step function in `kills` -- flat below a
threshold, then a sharp jump, a shape a single log1p-transformed linear
coefficient represents poorly), `monotonic_tree`/`boosted_stumps` reach
materially higher validation pairwise accuracy than `linear`/`gam` in
`tests/test_score_v2_compare.py`'s dedicated test -- demonstrating
non-linear candidates genuinely CAN win, not just that the code permits
it in principle.

**Split safety** matches the linear-only stage: an empty TRAIN split
returns `status="insufficient_data"` immediately (no fitting attempted at
all), never a silent fallback to the full dataset.

**Boosting's inner early-stop split (never the outer validation set).**
`fit_pairwise_boosted_stumps` supports validation-based early stopping in
addition to train-loss-based stopping -- but the split it uses for that
is a deterministic INNER `(fit, stop)` partition of the TRAIN dataset
ALONE, derived by
`score_v2.training.boosting.derive_inner_early_stop_split`, never the
outer validation/test split. Games are grouped into connected components
via their `PairLabel`s (so no pair, or transitively-linked chain of
pairs, is ever split across the inner boundary); whole groups -- never
individual records -- are assigned to inner-fit or inner-stop via a
fixed-seed deterministic shuffle, so the SAME train dataset always
produces a bit-identical inner split, whether during comparison or later
during artifact re-derivation for export
(`score_v2.training.compare.build_artifact_for_family` calls the exact
same fitting path, so its content_hash matches the comparison's selected
artifact exactly). If TRAIN has too few independent groups to safely form
both non-trivial inner sides, early stopping is honestly disabled
(fit on the whole TRAIN dataset, no validation-based stopping) rather
than reaching for the outer validation/test split. This keeps the OUTER
validation split an equally arms-length judge of every family -- boosting
never gets an early "peek" at it before cross-family selection, and
perturbing the outer validation or test split provably cannot change
boosting's fitted structure (see the dedicated leakage tests in
`tests/test_score_v2_compare.py`).

Inner-split viability uses the same abstention policy as fitting. A
partition whose fit or monitor side has no usable pair after abstained
records are excluded is rejected, and boosting instead fits the complete
TRAIN split without validation-based early stopping.

The inner monitor begins with the zero-stump baseline and the returned
ensemble is always restored to the best inner-stop checkpoint, including
zero stumps when every learned round makes held-out loss worse. This
applies even when training ends because of the round budget, no further
gain, or a train-loss plateau rather than the patience trigger itself.

CLI:

```
py scripts/score_v2/compare_models.py --dataset dataset.jsonl \
    --model-version 0.1.0-dev --calibration-version 0.1.0-dev \
    [--train-split train] [--validation-split validation] [--test-split test] \
    [--include-abstained] [--report-out report.json] \
    [--export-selected-dir DIR]
```

Prints one JSON comparison report per evidence tier. Never writes a
production artifact; `--export-selected-dir` is an explicit, clearly-
labeled opt-in that additionally saves the winning candidate's artifact
(always `production_ready=False`, comparison-only release notes) for
round-trip inspection -- not a release step.

**Known limitations of this comparison stage** (see also
`docs/SCORE_V2_MODEL_CARD_TEMPLATE.md`):

- The monotonic tree is a SINGLE tree (one greedy CART pass against a
  point-wise pairwise-pseudo-gradient proxy target), not an ensemble --
  deliberately, since `boosting.py` already covers "many simple additive
  weak learners" and a full iterative gradient-boosted-tree ensemble was
  judged unnecessary added complexity for this stage.
- Only `boosting.py` has an internal early-stopping mechanism (in
  addition to train-loss-based stopping), and it uses an INNER split of
  TRAIN alone (see "Boosting's inner early-stop split" above), never the
  outer validation split; GAM and the tree rely solely on the
  orchestrator's post-hoc validation-based selection and eligibility
  gating for overfitting protection.
- Boosting's inner early-stop split spends a fraction of TRAIN (~20% by
  default) purely on monitoring when to stop, so its effective fitting
  set -- and therefore its reported `n_pairs_used`/`n_items` -- is
  honestly smaller than the other three families', which each use the
  whole TRAIN split. This is the standard, expected tradeoff of any
  internal validation-based early-stopping mechanism, not a bug.
- The per-family minimum-pairs thresholds above are judgment calls, to be
  revisited once a real corpus exists at meaningful scale.
- No hyperparameter search is performed for any family (fixed defaults
  per family) -- with today's near-zero-label corpus, a search would have
  nothing meaningful to optimize against.

## Calibration (`score_v2/training/calibration.py`)

- **Role calibration**: `offset = mean(raw_linear_score | role) * (n / (n
  + shrinkage_k))`, `shrinkage_k=5.0` by default -- a role with few (or
  zero) training rows shrinks its offset toward 0 rather than trusting a
  noisy or nonexistent per-role mean.
- **Score mapping**: `score = 50 + 50 * tanh(adjusted / scale)`, bounded
  in `[0, 100]` by construction (`tanh` saturates). `scale` is the
  training set's own robust (median/MAD) spread of adjusted scores,
  falling back to a fixed default (`5.0`) only when there is no
  measurable spread (e.g. zero pairs).
- **Neutral variants** (`neutral_role_calibration`/
  `neutral_score_calibration`): every offset/shrinkage_weight is `0.0`
  and `scale` is the fixed default -- used by `train_tier`'s
  `"insufficient_data"` path (see above).
- **Generic, family-agnostic variants** (`fit_role_calibration_for_score_fn`/
  `fit_score_calibration_for_score_fn`): identical math, but parameterized
  by a plain `score_fn(record) -> float` instead of a linear-specific
  `FittedBaseline` -- this is what lets `score_v2.training.compare` role/
  score-calibrate a GAM/boosting/tree candidate exactly the same way the
  linear family already is. `fit_role_calibration`/`fit_score_calibration`
  are now thin, behavior-preserving wrappers around these.

## Runtime scoring (`score_v2/runtime.py`)

`score_participant(artifact, game_features, participant_id)` first
verifies `game_features["evidence_source"] == artifact.evidence_source`
(raising `EvidenceTierMismatchError` otherwise -- an artifact must never
score evidence from a tier it was not built for, even if a caller
bypasses `select_artifact`/`score_game`), then computes the raw model
score -- dispatched by `artifact.model_family` to a plain coefficient sum
(`linear`) or `score_v2.model_shapes.evaluate_gam_shapes`/
`evaluate_boosted_stumps`/`evaluate_tree` (the three non-linear
families) -- from present features only (missing features contribute
nothing for every family -- neutral, not a guessed value), subtracts the
role offset, maps through the score calibration, then:

- **Confidence** = `(1 - missing_feature_penalty * missing_fraction) *
  ((1 - evidence_quality_weight) + evidence_quality_weight *
  chosen_source_completeness)`, clamped to `[0, 1]`.
- **Uncertainty shrinkage toward 50**: `final_score = 50 + (raw_score -
  50) * confidence` -- the less trustworthy the number, the closer it is
  pulled to "average", never toward an arbitrary extreme.
- **Score interval**: half-width interpolates between
  `interval_min_half_width` (high confidence) and
  `interval_max_half_width` (low confidence).
- **Abstention reasons**: `short_game` (propagated from
  `game_features["abstain"]`), `insufficient_features` (present-feature
  fraction below a threshold), `low_confidence` (confidence below a
  threshold) -- reported as a list, never silently swallowed; the score
  is still computed (like `score_features.py`'s own `abstain` flag) so a
  caller can choose whether to withhold display.

**`rank_confidence` is a genuine group-level measure, computed only by
`score_game`, never by `score_participant`.** An earlier version exposed
`ScoreResult.rank_confidence` as a plain alias of the per-participant
`confidence` -- misleading, since "confidence in this participant's rank"
requires knowing every other participant's score, which a single-item
scorer cannot see. `score_game` now returns `dict[int, RankedScoreResult]`:
each result carries `rank` (1-indexed, score-descending) and a real
`rank_confidence` -- the minimum pairwise rank confidence against this
participant's immediate neighbors in the sorted order, itself derived
from each neighbor pair's score gap relative to their combined
score-interval half-widths (`1.0` = gap fully clears both intervals,
confidently separated; `0.0` = fully overlapping/indistinguishable; `1.0`
for a solo participant with no neighbor at all). `ScoreResult.confidence`
(per-participant evidence completeness) and `RankedScoreResult.rank_confidence`
(group-level rank certainty) are deliberately distinct fields.

Tier routing (`select_artifact`) is an exact-key lookup with no implicit
substitution -- see "Four separate evidence-tier artifacts" above.

## Training dataset schema (`score_v2/training/dataset.py`)

A JSONL file of two row kinds (plus one header row with real,
cross-checked `feature_record_count`/`pair_label_count`):

- `feature_record`: one participant's `score_features.py` block for one
  game/tier + its corpus split assignment. Validated for outcome leakage
  on every construction path (not just deserialization). Carries **two**
  identifiers:
  - `item_ref` (`"{game_id}:{participant_id}:{evidence_source}"`) --
    globally unique storage key, distinct per tier, so the SAME
    game/participant's evidence in multiple tiers at once (the normal
    case: `aggregate` is always present, alongside `lcu_timeline` once
    captured, etc.) can coexist in one `TrainingDataset` without a
    collision.
  - `base_ref` (`"{game_id}:{participant_id}"`) -- the tier-agnostic
    review reference, exactly matching `corpus.review.export_for_training`'s
    ref shape.
- `pair_label`: a de-blinded `corpus.review.export_for_training` row
  (`left_ref`/`right_ref` as `base_ref`s, `choice`/`confidence`/
  `rationale_tags`). Strictly validated on construction: `choice` must be
  one of `corpus.review`'s four values, `relation`/`winner_ref` must
  agree with `choice`, `confidence` in `[0, 1]`, `left_ref != right_ref`,
  non-empty `reviewer_id`. `TrainingDataset.validate()` additionally
  rejects a duplicate `(pair_id, reviewer_id)` (ambiguous double-counted
  supervision) while allowing distinct reviewers to rate the same pair.

A single human pairwise preference is expressed ONCE, in terms of
`base_ref`, and is resolved **independently per tier** by
`score_v2.training.export.dataset_for_tier` -- a pair only applies to a
given tier's training run if BOTH referenced participants actually have
a record in that tier; otherwise it is honestly excluded (counted in
`n_pairs_skipped`) for that tier's run, never silently collapsed onto
whichever tier happened to be filtered first.
`TrainingDataset.feature_records_by_base_ref()` enforces this by raising
if called on a dataset that still mixes multiple tiers.

`select_split(dataset, split_name)` restricts a dataset to one split; it
returns an **empty** dataset (never falls back to "everything") if no
record carries that split. `--split none` in the CLIs is the only
explicit all-data path.

`StateValueLabel` (game/team win-loss) is a **third, separate** type with
its own JSONL stream (`save_state_value_labels_jsonl`/
`load_state_value_labels_jsonl`) -- never merged into a `FeatureRecord`.
No state-value *model* is trained in this stage; it exists only so a
future external validity check ("do average team scores correlate with
who won?") has somewhere honest to live without ever touching
`score_v2.feature_spec`.

## Evaluation (`score_v2/training/evaluate.py`)

All stdlib, all honest-`None`-on-insufficient-data:

- **Pairwise accuracy** (overall + sliced by role/evidence tier/duration
  bucket via `slice_pairwise_accuracy`): fraction of decisive, scoreable
  pairs where the higher-scored item was preferred. A slice only ever
  assigns a pair when BOTH sides share the same key value -- a pair
  spanning two different roles (or tiers, or duration buckets) is never
  arbitrarily attributed to one side; it goes into an explicit `mixed`
  bucket instead (`SlicedPairwiseAccuracy.mixed`), and a pair referencing
  an unknown record is counted separately
  (`n_excluded_missing_record`), never silently dropped uncounted.
- **Spearman's rho / Kendall's tau-b**: computed over items that belong
  to a group (game) with enough reviewed pairs among its own members to
  imply a rank order via net pairwise wins -- never from game outcome.
- **Rank agreement**: exact-rank rate, within-one-rank rate,
  top/bottom-match rate, and mean NDCG, all against that same
  human-pairwise-implied per-game ranking.
- **Calibration**: Brier score and binned Expected Calibration Error
  against the pairwise logistic prediction `sigmoid(score_left -
  score_right)`.
- **Risk-coverage curve**: error rate at confidence-ordered coverage
  levels. Target item counts are deduplicated (and capped at the real
  item count) before building the curve -- with fewer items than
  `n_points`, a naive fixed grid would otherwise repeat the same `count`
  (and therefore the same `coverage`/`risk`) at multiple "distinct"
  points, implying more granularity than the data supports.
- **Bootstrap stability**: pairwise review labels from the same game are
  NOT independent observations (they share participants/context), so
  `bootstrap_pairs_by_game` resamples whole **games** with replacement (a
  cluster/block bootstrap), not individual pairs -- each resample's pair
  list is the concatenation of every pair belonging to each resampled
  game. A fixed-seed `random.Random`, never the shared global RNG --
  identical `(items, seed, n_resamples)` always yields an identical
  result. (A plain i.i.d. `bootstrap_stability` also exists for
  genuinely independent items elsewhere.)

## CLIs (`scripts/score_v2/*.py`)

```
py scripts/score_v2/build_training_dataset.py \
    --history-db <path> --manifest <corpus_manifest.json> \
    --split-seed <seed> --output dataset.jsonl \
    [--labels labels.jsonl --token-map token_map.json]

py scripts/score_v2/train_model.py \
    --dataset dataset.jsonl --output-dir artifacts/dev \
    --model-version 0.1.0-dev --calibration-version 0.1.0-dev \
    [--split train|validation|test|none] [--include-abstained]

py scripts/score_v2/evaluate_model.py \
    --dataset dataset.jsonl --artifacts-dir artifacts/dev \
    [--split validation|train|test|none] [--report-out report.json]

py scripts/score_v2/compare_models.py \
    --dataset dataset.jsonl --model-version 0.1.0-dev \
    --calibration-version 0.1.0-dev \
    [--train-split train] [--validation-split validation] [--test-split test] \
    [--include-abstained] [--report-out report.json] [--export-selected-dir DIR]

py -m score_v2.training.dataset validate <dataset.jsonl>
```

`train_model.py` writes one immutable, hashed `artifacts/<tier>.json` per
evidence tier present in the dataset, all `production_ready: false`
(linear family only -- see "Model family comparison" above for the other
three families). `evaluate_model.py` loads (and hash-verifies) those
artifacts and scores every matching record through the real
`score_v2.runtime.score_participant` path, so evaluation reflects exactly
what the shipped runtime would compute. `compare_models.py` fits and
compares all four families per tier, selecting on validation metrics only
(see "Model family comparison"); it never writes any artifact to disk
unless `--export-selected-dir` is explicitly passed. **`train_model.py`,
`evaluate_model.py`, and `compare_models.py` (via its required, always-
distinct train/validation/test splits) all FAIL or honestly report
`insufficient_data` if a requested split matches zero records** -- none
silently falls back to the full dataset; `--split none` is the only
explicit "use every record" path (for `train_model.py`/`evaluate_model.py`
only -- `compare_models.py` intrinsically needs three genuinely disjoint
splits, so it has no `none` escape hatch).

## Tests

`tests/test_score_v2_leakage.py`, `test_score_v2_feature_spec.py`,
`test_score_v2_artifact.py`, `test_score_v2_runtime.py`,
`test_score_v2_dataset.py`, `test_score_v2_baseline.py`,
`test_score_v2_calibration.py`, `test_score_v2_evaluate.py`,
`test_score_v2_export.py`, `test_score_v2_scripts.py`,
`test_score_v2_adversarial_cases.py`, `test_score_v2_monotonic_utils.py`,
`test_score_v2_model_shapes.py`, `test_score_v2_gam.py`,
`test_score_v2_boosting.py`, `test_score_v2_tree.py`, and
`test_score_v2_compare.py` (394 tests total) cover: recursive leakage
rejection (including the `lead_windows`/"windows" false-positive
regression), deterministic artifact hashing/tamper/rehashed/malformed
rejection (all four model families, including the tree's independent
structural `verify_tree_monotonicity` re-check), exact per-tier
feature-contract matching (full coverage for linear, valid subset for the
three non-linear families), monotonic coefficient/knot/stump/tree-split
invariants (with no trained/phantom intercept for any family), honest
convergence (`None`/real-criterion, never fabricated for a family with no
applicable stopping rule), role-calibration shrinkage and abstain
exclusion (linear-specific and the new generic `_for_score_fn` siblings,
proven behavior-identical), missing-feature confidence reduction
(reflecting only the features each family actually uses),
abstention (short game, insufficient features, low confidence),
participant/dict-order invariance, tier-routing rejection of implicit
substitution, evidence-tier mismatch rejection at score time, genuine
group-level rank confidence, multi-tier record coexistence, `PairLabel`
construction validation, homogeneous-only slice evaluation,
game-clustered bootstrap, pairwise-loss training correction on separable
synthetic data (all four families), a genuine feature-INTERACTION signal
only the tree can capture, monotonic counterfactual invariants
(perturbing one feature never regresses a fitted model's score, for
every family), leakage-safe validation/test normalization (never
re-derived from held-out data, verified against a held-out split with a
deliberately extreme, different distribution), deterministic 4-family
selection (bit-identical winner across repeated runs), empirically
demonstrated non-linear-beats-linear on a genuinely non-linear synthetic
signal, per-family minimum-data eligibility gating (including the tree's
structural degenerate-single-leaf case), "no model wins on training
metrics alone", honest current-corpus no-selection behavior, every
evaluation metric, real end-to-end CLI runs (with both zero and
synthetic pairwise labels, split-safety failure, malformed/tampered
artifact rejection, and `compare_models.py`'s honest no-selection/
deterministic-report/export-nothing-when-unselected behavior),
boosting's inner early-stop split (grouped/deterministic/leak-free, honest
disabling on tiny train, and -- the key regression guard -- perturbing
the OUTER validation or test split provably cannot change boosting's
fitted structure, content hash, or runtime predictions, and an
early-stopped boosting winner re-derives and exports with an identical
hash), and the two verified adversarial cases (Sion 8:30 short game;
K'Sante/Seraphine/Vel'Koz) represented honestly -- one test shows the
pipeline genuinely resolves the case given supervision, a second
explicitly shows today's real, unlabeled corpus does not yet (a tie, not
a fabricated discrimination). All fixtures are synthetic or the existing
sanitized `tests/fixtures/` data; no secrets or real identities.
