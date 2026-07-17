# DAEMON Score v2 Model Card Template

Copy this template into a new `MODEL_CARD_<tier>_<model_version>.md` (or
paste it into the training run's notes) every time an artifact is a
candidate for wider use. **Every field must be filled in honestly from
the artifact's own `training_metadata`/`evaluation_metadata` -- do not
copy numbers from a different tier or a different run.**

## 1. Identity

| Field | Value |
|---|---|
| Evidence tier (`evidence_source`) | `match_v5` / `lcu_timeline` / `live_client` / `aggregate` |
| `model_version` | |
| `feature_version` | must match `score_features.FEATURE_VERSION` used to build the training dataset |
| `calibration_version` | |
| `content_hash` | (first 12 hex chars is enough to identify; full hash lives in the artifact file) |
| Fallback/shrinkage | `fallback.is_fallback` / `fallback.shrinkage_source`, or "none" |

## 2. Pipeline-ready vs. production-trained

**Circle one, and only one may ever be true:**

- [ ] **Pipeline-ready development artifact.** Produced by
  `scripts/score_v2/train_model.py` against the current local/tiny
  corpus. `production_ready` is `False` in the artifact itself.
  `training_metadata.status` is `"insufficient_data"` or `"fitted"` on a
  sample too small to trust for real ranking decisions. This is the
  status of every artifact this pipeline has produced as of this stage.
- [ ] **Production-trained.** Every gate in section 5 below has been
  independently checked and documented with evidence (not just asserted).
  Only in this case may a human editor also flip `production_ready` to
  `True` in a *new* artifact build (the pipeline code itself never sets
  this automatically -- see `score_v2.training.export.train_tier`).

If you are unsure which box to check, it is pipeline-ready.

## 3. Training data summary (from `training_metadata`)

| Field | Value |
|---|---|
| `n_items` (participant feature records) | |
| `n_pairs_used` | |
| `n_pairs_skipped` (tie/insufficient_evidence/unmatched-ref) | |
| `l2_lambda` / `learning_rate` / `iterations_run` | |
| `converged` | |
| Split used for training (`train` / `none`) | |
| Corpus manifest source / date range | |

## 4. Evaluation summary (from `evaluate_model.py`'s report, on the
   **validation** split unless noted)

| Metric | Value | n | Honest interpretation |
|---|---|---|---|
| Pairwise accuracy (overall) | | | `None` means insufficient decisive pairs -- do not treat as 50%. |
| Pairwise accuracy by role | | | |
| Pairwise accuracy by evidence tier | | | |
| Pairwise accuracy by duration bucket | | | |
| Spearman's rho | | | `None` if fewer than 2 samples or zero variance. |
| Kendall's tau-b | | | |
| Rank agreement (exact / within-one / top / bottom / NDCG) | | | Only computed over games with >= `min_group_pairs` reviewed pairs among their own participants. |
| Brier score | | | Lower is better; 0.25 is the "always predict 0.5" baseline. |
| Expected Calibration Error | | | `None` if fewer than `n_bins` predictions. |
| Bootstrap stability (mean/std of pairwise accuracy) | | | Wide std = don't trust the point estimate. |

## 5. Release gates (ALL must be independently checked before
   `production_ready` may ever be set `True` for any artifact)

- [ ] **Real Match-V5 authorization unblocked and cross-verified.** The
  vault decision "Gate final Score v2 validation on Match-V5
  authorization, not local feature development" is resolved: a currently
  authorized Riot key has been used to pull and cross-validate Match-V5
  evidence against LCU aggregates/timelines for a representative sample
  (see the `score-v2-match-v5-verification` blocker).
- [ ] **Sufficient real, human-reviewed pairwise labels.** Not synthetic
  test fixtures -- real blinded pairwise reviews via `corpus/review.py`,
  covering multiple reviewers, multiple roles, multiple champions, and a
  range of match durations. A specific minimum sample size and
  inter-rater agreement threshold (Cohen's kappa) must be set and met
  before this box is checked; a single-digit number of labels is never
  enough regardless of what `min_pairs_for_nontrivial_fit` was configured
  to for a development run.
- [ ] **Leakage re-verified on the actual training run**, not just
  assumed from the code review of `score_v2.leakage`/`score_v2.feature_spec`
  (`assert_no_outcome_leakage` raised zero problems across every
  `FeatureRecord` actually used).
- [ ] **All 11 adversarial cases in `corpus/data/adversarial_cases.json`
  evaluated**, including the two `verified_local` cases, with `passed`
  either `True` or independently reviewed if `None`/`False`. A vacuous
  pass (e.g. every score tied, satisfying only a `min_gap=0.0`
  tie-tolerant expectation) does not count -- see
  `tests/test_score_v2_adversarial_cases.py` for what a non-vacuous
  resolution looks like.
- [ ] **Calibration checked on held-out data**, not the training split:
  Brier/ECE computed on `validation` or `test`, not `train`.
- [ ] **Bootstrap stability is tight enough to trust**, i.e. the std
  reported by `bootstrap_stability` is small relative to the metric's
  own scale -- a specific threshold should be set and documented per
  tier before this box is checked.
- [ ] **Independent human/code review** of the specific trained artifact
  (not just this pipeline's code) has signed off, separate from whoever
  ran the training.
- [ ] **`score-v2-routing`/`score-v2-shadow`/`score-v2-validation` SQL
  todos are complete or explicitly scoped out** for this release (this
  pipeline stage does not touch routing/shadowing/UI -- see
  `docs/SCORE_V2_MODELS.md` scope note).

## 6. Known limitations (fill in per artifact; do not leave blank)

- Corpus size and diversity at training time (region/rank/champion
  coverage -- `history_store.py` does not currently capture region or
  rank tier at all, see `docs/CORPUS_AND_REVIEW.md`).
- Which evidence-tier capabilities were actually exercised (e.g. an
  `aggregate`-tier artifact never sees `vision_actionable_rate` or
  `resource_conversion_rate` -- those features are always "missing" for
  every training row of that tier, which the fitted coefficients and
  confidence penalty both already reflect, but call it out explicitly).
- Any role/champion with zero or near-zero training samples (role
  calibration will have shrunk their offset to ~0, i.e. "no adjustment",
  not "verified fair").
- Whether `production_ready` is `True` anywhere downstream that consumes
  this artifact, and what happens if it is later revoked.

## 7. Sign-off

| Role | Name | Date |
|---|---|---|
| Trained by | | |
| Evaluated by | | |
| Independently reviewed by (required for production-trained only) | | |
