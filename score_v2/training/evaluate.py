"""Deterministic grouped evaluation utilities for DAEMON Score v2.

Every metric here is computed from the corpus's own blinded pairwise
review labels (`score_v2.training.dataset.PairLabel`) -- never from game
outcome. When a metric cannot be honestly computed (too few samples, a
degenerate/zero-variance input, an unsupported tie-heavy shape for a rank
correlation), the function returns `None` rather than a fabricated number;
callers must treat `None` as "insufficient data", not "zero" or "perfect".

Grouping utilities (`slice_pairwise_accuracy`, `duration_bucket`) only
ever assign a PAIR to a slice (role/evidence tier/duration bucket) when
BOTH sides of the pair share that same key -- a pair spanning two
different roles (or tiers, or duration buckets) goes into an explicit
`mixed` bucket instead of being arbitrarily attributed to one side.
Bootstrap resampling (`bootstrap_pairs_by_game`) resamples whole GAMES,
not individual pairs, since pairs from the same game are not independent
observations.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

from score_v2.training.baseline import sigmoid
from score_v2.training.dataset import FeatureRecord, PairLabel, TrainingDataset, parse_base_ref

ScoreFn = Callable[[FeatureRecord], float]


def score_all(records: Sequence[FeatureRecord], score_fn: ScoreFn) -> dict[str, float]:
    """Score every record, keyed by `base_ref` -- matching `PairLabel`
    refs, so the result is directly usable by `pairwise_accuracy` and
    friends without any further translation.
    """
    return {record.base_ref: score_fn(record) for record in records}


# ── pairwise accuracy ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PairwiseAccuracyResult:
    accuracy: Optional[float]
    n_scored: int
    n_ties_excluded: int
    n_unscoreable: int

    def to_dict(self) -> dict:
        return {
            "accuracy": self.accuracy, "n_scored": self.n_scored,
            "n_ties_excluded": self.n_ties_excluded,
            "n_unscoreable": self.n_unscoreable,
        }


def pairwise_accuracy(
        pairs: Sequence[PairLabel], scores: Mapping[str, float]) -> PairwiseAccuracyResult:
    """Fraction of decisive pairs where the higher-scored item was preferred.

    Ties (label choice `"tie"`, or an exact score tie) and
    `"insufficient_evidence"` labels are excluded from the accuracy
    denominator (counted separately) since there is no directional
    prediction to be right or wrong about. `accuracy` is `None` when zero
    decisive, scoreable pairs exist.
    """
    correct = 0
    scored = 0
    ties = 0
    unscoreable = 0
    for pair in pairs:
        if pair.choice not in ("left", "right"):
            ties += 1
            continue
        if pair.left_ref not in scores or pair.right_ref not in scores:
            unscoreable += 1
            continue
        left_score = scores[pair.left_ref]
        right_score = scores[pair.right_ref]
        if left_score == right_score:
            ties += 1
            continue
        predicted_left_wins = left_score > right_score
        actual_left_wins = pair.choice == "left"
        if predicted_left_wins == actual_left_wins:
            correct += 1
        scored += 1
    accuracy = (correct / scored) if scored else None
    return PairwiseAccuracyResult(
        accuracy=accuracy, n_scored=scored, n_ties_excluded=ties,
        n_unscoreable=unscoreable,
    )


# ── rank correlations (stdlib only) ─────────────────────────────────────────

def _average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = average_rank
        i = j + 1
    return ranks


def _pearson(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    n = len(a)
    if n < 2:
        return None
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a <= 0.0 or var_b <= 0.0:
        return None
    return cov / ((var_a ** 0.5) * (var_b ** 0.5))


def spearman_rank_correlation(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """Spearman's rho via Pearson correlation of average ranks.

    `None` for fewer than 2 paired samples, or when either side has zero
    variance (undefined, not zero).
    """
    if len(a) != len(b) or len(a) < 2:
        return None
    return _pearson(_average_ranks(a), _average_ranks(b))


def kendall_tau(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """Kendall's tau-b (ties-corrected), O(n^2) -- fine at corpus scale.

    `None` for fewer than 2 samples, or when one side is fully tied (a
    zero denominator -- undefined, not zero).
    """
    n = len(a)
    if n != len(b) or n < 2:
        return None
    concordant = discordant = ties_a_only = ties_b_only = ties_both = 0
    for i in range(n):
        for j in range(i + 1, n):
            da = a[i] - a[j]
            db = b[i] - b[j]
            if da == 0 and db == 0:
                ties_both += 1
            elif da == 0:
                ties_a_only += 1
            elif db == 0:
                ties_b_only += 1
            elif (da > 0) == (db > 0):
                concordant += 1
            else:
                discordant += 1
    n0 = n * (n - 1) / 2
    n1 = n0 - ties_a_only - ties_both
    n2 = n0 - ties_b_only - ties_both
    if n1 <= 0 or n2 <= 0:
        return None
    return (concordant - discordant) / ((n1 * n2) ** 0.5)


# ── rank agreement (NDCG / top-bottom / exact-or-within-one) ───────────────

def _wins_losses_from_pairs(
        base_refs: Sequence[str], pairs: Sequence[PairLabel]) -> dict[str, float]:
    """Aggregate a group's pairwise labels into a simple net-wins tally.

    Builds a human-implied rank ordering for a *group* of items (e.g. all
    ten participants of one game) that has enough reviewed pairs among its
    own members -- never derived from game outcome.
    """
    tally = {ref: 0.0 for ref in base_refs}
    members = set(base_refs)
    for pair in pairs:
        if pair.left_ref not in members or pair.right_ref not in members:
            continue
        if pair.choice == "left":
            tally[pair.left_ref] += 1.0
            tally[pair.right_ref] -= 1.0
        elif pair.choice == "right":
            tally[pair.right_ref] += 1.0
            tally[pair.left_ref] -= 1.0
    return tally


def _ranks_from_scores(scores: Mapping[str, float]) -> dict[str, int]:
    """1 = highest score. Ties broken by ref for determinism."""
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return {ref: index + 1 for index, (ref, _) in enumerate(ordered)}


def _dcg(relevances: Sequence[float]) -> float:
    return sum(
        (2 ** relevance - 1) / math.log2(index + 2)
        for index, relevance in enumerate(relevances)
    )


def _ndcg_for_group(
        predicted_order: Sequence[str], relevance: Mapping[str, float]) -> Optional[float]:
    if len(predicted_order) < 2:
        return None
    actual = _dcg([relevance[ref] for ref in predicted_order])
    ideal_order = sorted(predicted_order, key=lambda ref: -relevance[ref])
    ideal = _dcg([relevance[ref] for ref in ideal_order])
    if ideal <= 0.0:
        return None
    return actual / ideal


@dataclass(frozen=True)
class RankAgreementResult:
    n_groups: int
    n_items: int
    exact_rate: Optional[float]
    within_one_rate: Optional[float]
    top_match_rate: Optional[float]
    bottom_match_rate: Optional[float]
    mean_ndcg: Optional[float]

    def to_dict(self) -> dict:
        return {
            "n_groups": self.n_groups, "n_items": self.n_items,
            "exact_rate": self.exact_rate, "within_one_rate": self.within_one_rate,
            "top_match_rate": self.top_match_rate,
            "bottom_match_rate": self.bottom_match_rate, "mean_ndcg": self.mean_ndcg,
        }


def rank_agreement(
        groups: Mapping[str, Sequence[str]], pairs: Sequence[PairLabel],
        scores: Mapping[str, float], *, min_group_pairs: int = 3) -> RankAgreementResult:
    """Compare predicted rank order against a human-pairwise-implied order.

    `groups` maps a group key (typically `game_id` as a string) to the
    `base_ref`s belonging to that group. A group only contributes if it
    has at least `min_group_pairs` reviewed decisive pairs among its own
    members AND at least two scored members -- otherwise it is skipped
    (not counted as a failure, never padded with a guess).
    """
    exact_hits = within_one_hits = top_hits = bottom_hits = 0
    ndcg_values: list[float] = []
    considered_groups = 0
    considered_items = 0

    for group_refs in groups.values():
        member_refs = [ref for ref in group_refs if ref in scores]
        if len(member_refs) < 2:
            continue
        group_pairs = [
            pair for pair in pairs
            if pair.left_ref in member_refs and pair.right_ref in member_refs
            and pair.choice in ("left", "right")
        ]
        if len(group_pairs) < min_group_pairs:
            continue
        tally = _wins_losses_from_pairs(member_refs, group_pairs)
        human_rank = _ranks_from_scores(tally)
        predicted_rank = _ranks_from_scores({ref: scores[ref] for ref in member_refs})

        considered_groups += 1
        considered_items += len(member_refs)
        n = len(member_refs)
        for ref in member_refs:
            delta = abs(human_rank[ref] - predicted_rank[ref])
            if delta == 0:
                exact_hits += 1
            if delta <= 1:
                within_one_hits += 1
        if min(human_rank, key=human_rank.get) == min(predicted_rank, key=predicted_rank.get):
            top_hits += 1
        if max(human_rank, key=human_rank.get) == max(predicted_rank, key=predicted_rank.get):
            bottom_hits += 1

        relevance = {ref: float(n - human_rank[ref]) for ref in member_refs}
        predicted_order = sorted(member_refs, key=lambda ref: predicted_rank[ref])
        ndcg = _ndcg_for_group(predicted_order, relevance)
        if ndcg is not None:
            ndcg_values.append(ndcg)

    if considered_groups == 0:
        return RankAgreementResult(
            n_groups=0, n_items=0, exact_rate=None, within_one_rate=None,
            top_match_rate=None, bottom_match_rate=None, mean_ndcg=None,
        )
    return RankAgreementResult(
        n_groups=considered_groups, n_items=considered_items,
        exact_rate=exact_hits / considered_items,
        within_one_rate=within_one_hits / considered_items,
        top_match_rate=top_hits / considered_groups,
        bottom_match_rate=bottom_hits / considered_groups,
        mean_ndcg=(statistics.fmean(ndcg_values) if ndcg_values else None),
    )


# ── calibration metrics (Brier / ECE) ───────────────────────────────────────

def brier_score(predictions: Sequence[tuple[float, float]]) -> Optional[float]:
    """Mean squared error between predicted P(left wins) and actual target.

    `predictions` is `(predicted_probability, actual_target)` pairs
    (`actual_target` in `{0.0, 0.5, 1.0}`, matching
    `score_v2.training.baseline`'s tie handling). `None` if empty.
    """
    if not predictions:
        return None
    return statistics.fmean((p - t) ** 2 for p, t in predictions)


def expected_calibration_error(
        predictions: Sequence[tuple[float, float]], *, n_bins: int = 10) -> Optional[float]:
    """Standard binned ECE: |confidence - accuracy| weighted by bin size.

    `predictions` is `(predicted_probability, actual_binary_outcome)`
    pairs with `actual_binary_outcome` in `{0.0, 1.0}` (exclude ties --
    ECE is not well-defined for a 0.5 target). `None` if fewer than
    `n_bins` predictions are supplied (too coarse to bin honestly).
    """
    if len(predictions) < n_bins:
        return None
    bins: list[list[tuple[float, float]]] = [[] for _ in range(n_bins)]
    for prob, target in predictions:
        index = min(n_bins - 1, int(prob * n_bins))
        bins[index].append((prob, target))
    total = len(predictions)
    error = 0.0
    for bucket in bins:
        if not bucket:
            continue
        confidence = statistics.fmean(p for p, _ in bucket)
        accuracy = statistics.fmean(t for _, t in bucket)
        error += (len(bucket) / total) * abs(confidence - accuracy)
    return error


# ── risk-coverage ────────────────────────────────────────────────────────────

def risk_coverage_curve(
        items: Sequence[tuple[float, bool]], *, n_points: int = 10) -> Optional[list[dict]]:
    """Selective-prediction curve: risk (error rate) at each coverage level.

    `items` is `(confidence, is_correct)` pairs. Sorted by confidence
    descending. Target item counts are deduplicated (and capped at
    `len(items)`) before building the curve -- with fewer items than
    `n_points`, a naive fixed grid would otherwise repeat the same
    `count` (and therefore the same `coverage`/`risk`) at multiple
    "distinct" points, implying more granularity than the data supports.
    `None` if `items` is empty.
    """
    if not items:
        return None
    ordered = sorted(items, key=lambda pair: -pair[0])
    n = len(ordered)
    target_counts = sorted({
        max(1, round(step / n_points * n)) for step in range(1, n_points + 1)
    })
    curve = []
    for count in target_counts:
        subset = ordered[:count]
        risk = 1.0 - statistics.fmean(1.0 if correct else 0.0 for _, correct in subset)
        curve.append({
            "coverage": count / n, "risk": risk, "n_items": count,
            "min_confidence_in_subset": subset[-1][0],
        })
    return curve


# ── slicing ──────────────────────────────────────────────────────────────────

def duration_bucket(duration_seconds: float) -> str:
    if duration_seconds < 600:
        return "short_under_10m"
    if duration_seconds < 1500:
        return "normal_10_25m"
    if duration_seconds < 2400:
        return "long_25_40m"
    return "very_long_over_40m"


@dataclass(frozen=True)
class SlicedPairwiseAccuracy:
    """Pairwise accuracy grouped by a homogeneous key (role/tier/duration).

    A pair is only assigned into `by_key[k]` when BOTH its `left_ref` and
    `right_ref` records share the same key value `k` -- a pair spanning
    two different keys (e.g. comparing a "top" laner against a "jungle"
    laner in a role slice) is never arbitrarily attributed to one side;
    it goes into `mixed` instead. `n_excluded_missing_record` counts pairs
    where a referenced `base_ref` has no known record at all (so no key
    could be determined for either side).
    """

    by_key: Mapping[str, PairwiseAccuracyResult]
    mixed: PairwiseAccuracyResult
    n_excluded_missing_record: int

    def to_dict(self) -> dict:
        return {
            "by_key": {key: value.to_dict() for key, value in self.by_key.items()},
            "mixed": self.mixed.to_dict(),
            "n_excluded_missing_record": self.n_excluded_missing_record,
        }


def slice_pairwise_accuracy(
        pairs: Sequence[PairLabel], scores: Mapping[str, float],
        records_by_base_ref: Mapping[str, FeatureRecord],
        *, key_fn: Callable[[FeatureRecord], str]) -> SlicedPairwiseAccuracy:
    """Group `pairs` by `key_fn(record)`, requiring BOTH sides to agree.

    `records_by_base_ref` looks each pair's `left_ref`/`right_ref` up
    directly (e.g. per role, evidence tier, duration bucket, or any other
    caller-supplied grouping, including an externally supplied "did this
    team win" lookup for an external validity check -- since that
    grouping never touches `score_v2.feature_spec`).
    """
    grouped: dict[str, list[PairLabel]] = {}
    mixed: list[PairLabel] = []
    excluded_missing_record = 0
    for pair in pairs:
        left_record = records_by_base_ref.get(pair.left_ref)
        right_record = records_by_base_ref.get(pair.right_ref)
        if left_record is None or right_record is None:
            excluded_missing_record += 1
            continue
        left_key, right_key = key_fn(left_record), key_fn(right_record)
        if left_key == right_key:
            grouped.setdefault(left_key, []).append(pair)
        else:
            mixed.append(pair)
    by_key = {key: pairwise_accuracy(group, scores) for key, group in grouped.items()}
    return SlicedPairwiseAccuracy(
        by_key=by_key, mixed=pairwise_accuracy(mixed, scores),
        n_excluded_missing_record=excluded_missing_record,
    )


# ── bootstrap stability ──────────────────────────────────────────────────────

def bootstrap_stability(
        items: Sequence, metric_fn: Callable[[Sequence], Optional[float]],
        *, n_resamples: int = 200, seed: int = 1337) -> Optional[dict]:
    """Resample `items` with replacement `n_resamples` times (fixed `seed`)
    and report the mean/std/min/max of `metric_fn` across resamples.

    Deterministic for a given `(items, seed, n_resamples)` -- uses its own
    `random.Random(seed)` instance, never the shared global RNG. `None` if
    `items` is empty or every resample's metric is `None` (metric
    fundamentally unsupported for this data, not just noisy).

    This is a plain i.i.d. item-level bootstrap -- appropriate for
    independent items. Pairwise review labels from the same game are NOT
    independent of each other; use `bootstrap_pairs_by_game` for those.
    """
    if not items:
        return None
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(n_resamples):
        resample = [items[rng.randrange(len(items))] for _ in range(len(items))]
        value = metric_fn(resample)
        if value is not None:
            values.append(value)
    if not values:
        return None
    return {
        "mean": statistics.fmean(values),
        "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
        "n_resamples": n_resamples,
        "n_resamples_with_value": len(values),
    }


def _game_id_for_pair(pair: PairLabel) -> str:
    """Practical cluster key for a pair: the LEFT ref's game id.

    Reviewed pairs are expected to compare participants within the same
    game (the corpus review workflow's typical intent); if `left_ref` and
    `right_ref` ever belong to different games, this still assigns the
    pair a single, deterministic cluster (the left game) rather than
    attempting to model a genuinely cross-game dependency structure.
    """
    game_id, _ = parse_base_ref(pair.left_ref)
    return str(game_id)


def bootstrap_pairs_by_game(
        pairs: Sequence[PairLabel], metric_fn: Callable[[Sequence[PairLabel]], Optional[float]],
        *, n_resamples: int = 200, seed: int = 1337) -> Optional[dict]:
    """Cluster (block) bootstrap over GAMES, not individual pairs.

    Pairwise labels drawn from the same game share participants and
    context and are not independent observations -- resampling at the
    pair level (as a plain i.i.d. bootstrap would) understates the true
    variance. This resamples GAME clusters with replacement instead: each
    resample's pair list is the concatenation of every pair belonging to
    each resampled game (with repeats), and `metric_fn` is evaluated once
    per resample.
    """
    if not pairs:
        return None
    pairs_by_game: dict[str, list[PairLabel]] = {}
    for pair in pairs:
        pairs_by_game.setdefault(_game_id_for_pair(pair), []).append(pair)
    game_ids = sorted(pairs_by_game)
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(n_resamples):
        resample_pairs: list[PairLabel] = []
        for _ in range(len(game_ids)):
            game_id = game_ids[rng.randrange(len(game_ids))]
            resample_pairs.extend(pairs_by_game[game_id])
        value = metric_fn(resample_pairs)
        if value is not None:
            values.append(value)
    if not values:
        return None
    return {
        "mean": statistics.fmean(values),
        "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
        "n_resamples": n_resamples,
        "n_resamples_with_value": len(values),
        "n_groups": len(game_ids),
    }


# ── top-level orchestration ─────────────────────────────────────────────────

@dataclass(frozen=True)
class EvaluationReport:
    n_items: int
    n_pairs: int
    pairwise_accuracy_overall: PairwiseAccuracyResult
    pairwise_accuracy_by_role: SlicedPairwiseAccuracy
    pairwise_accuracy_by_evidence_source: SlicedPairwiseAccuracy
    pairwise_accuracy_by_duration_bucket: SlicedPairwiseAccuracy
    spearman: Optional[float]
    kendall: Optional[float]
    rank_agreement: RankAgreementResult
    brier: Optional[float]
    ece: Optional[float]
    risk_coverage: Optional[list]
    bootstrap_pairwise_accuracy: Optional[dict]

    def to_dict(self) -> dict:
        return {
            "n_items": self.n_items, "n_pairs": self.n_pairs,
            "pairwise_accuracy_overall": self.pairwise_accuracy_overall.to_dict(),
            "pairwise_accuracy_by_role": self.pairwise_accuracy_by_role.to_dict(),
            "pairwise_accuracy_by_evidence_source": (
                self.pairwise_accuracy_by_evidence_source.to_dict()
            ),
            "pairwise_accuracy_by_duration_bucket": (
                self.pairwise_accuracy_by_duration_bucket.to_dict()
            ),
            "spearman": self.spearman, "kendall": self.kendall,
            "rank_agreement": self.rank_agreement.to_dict(),
            "brier": self.brier, "ece": self.ece,
            "risk_coverage": self.risk_coverage,
            "bootstrap_pairwise_accuracy": self.bootstrap_pairwise_accuracy,
        }


def evaluate_dataset(
        dataset: TrainingDataset, scores: Mapping[str, float],
        confidences: Optional[Mapping[str, float]] = None,
        *, min_group_pairs: int = 3, bootstrap_seed: int = 1337,
        bootstrap_resamples: int = 200) -> EvaluationReport:
    """Compute the full grouped evaluation suite for one scored tier/split.

    `dataset` must be restricted to a SINGLE evidence tier (see
    `score_v2.training.export.dataset_for_tier`) -- `scores` and
    `confidences` are both keyed by `base_ref`, typically produced by
    running `score_v2.runtime.score_participant` (or
    `score_v2.training.calibration.raw_linear_score` for a
    pre-calibration view) over every `dataset.feature_records` entry.
    """
    records_by_base_ref = dataset.feature_records_by_base_ref()
    pairs = list(dataset.pair_labels)

    overall = pairwise_accuracy(pairs, scores)
    by_role = slice_pairwise_accuracy(
        pairs, scores, records_by_base_ref, key_fn=lambda record: record.role,
    )
    by_evidence = slice_pairwise_accuracy(
        pairs, scores, records_by_base_ref, key_fn=lambda record: record.evidence_source,
    )
    by_duration = slice_pairwise_accuracy(
        pairs, scores, records_by_base_ref,
        key_fn=lambda record: duration_bucket(record.duration_seconds),
    )

    groups: dict[str, list[str]] = {}
    for record in dataset.feature_records:
        groups.setdefault(str(record.game_id), []).append(record.base_ref)

    agreement = rank_agreement(groups, pairs, scores, min_group_pairs=min_group_pairs)

    model_values: list[float] = []
    human_values: list[float] = []
    for group_refs in groups.values():
        member_refs = [ref for ref in group_refs if ref in scores]
        if len(member_refs) < 2:
            continue
        group_pairs = [
            pair for pair in pairs
            if pair.left_ref in member_refs and pair.right_ref in member_refs
            and pair.choice in ("left", "right")
        ]
        if len(group_pairs) < min_group_pairs:
            continue
        tally = _wins_losses_from_pairs(member_refs, group_pairs)
        for ref in member_refs:
            model_values.append(scores[ref])
            human_values.append(tally[ref])

    spearman = spearman_rank_correlation(model_values, human_values)
    kendall = kendall_tau(model_values, human_values)

    calibration_predictions: list[tuple[float, float]] = []
    for pair in pairs:
        if pair.left_ref not in scores or pair.right_ref not in scores:
            continue
        prob = sigmoid(scores[pair.left_ref] - scores[pair.right_ref])
        if pair.choice == "left":
            calibration_predictions.append((prob, 1.0))
        elif pair.choice == "right":
            calibration_predictions.append((prob, 0.0))
        elif pair.choice == "tie":
            calibration_predictions.append((prob, 0.5))

    brier = brier_score(calibration_predictions)
    ece = expected_calibration_error(
        [(p, t) for p, t in calibration_predictions if t in (0.0, 1.0)]
    )

    risk_items: list[tuple[float, bool]] = []
    if confidences:
        for pair in pairs:
            if pair.choice not in ("left", "right"):
                continue
            if pair.left_ref not in scores or pair.right_ref not in scores:
                continue
            predicted_left_wins = scores[pair.left_ref] > scores[pair.right_ref]
            actual_left_wins = pair.choice == "left"
            confidence = min(
                confidences.get(pair.left_ref, 0.0),
                confidences.get(pair.right_ref, 0.0),
            )
            risk_items.append((confidence, predicted_left_wins == actual_left_wins))
    risk_curve = risk_coverage_curve(risk_items) if risk_items else None

    bootstrap = (
        bootstrap_pairs_by_game(
            pairs, lambda sample: pairwise_accuracy(sample, scores).accuracy,
            n_resamples=bootstrap_resamples, seed=bootstrap_seed,
        )
        if pairs else None
    )

    return EvaluationReport(
        n_items=len(records_by_base_ref), n_pairs=len(pairs),
        pairwise_accuracy_overall=overall, pairwise_accuracy_by_role=by_role,
        pairwise_accuracy_by_evidence_source=by_evidence,
        pairwise_accuracy_by_duration_bucket=by_duration,
        spearman=spearman, kendall=kendall, rank_agreement=agreement,
        brier=brier, ece=ece, risk_coverage=risk_curve,
        bootstrap_pairwise_accuracy=bootstrap,
    )
