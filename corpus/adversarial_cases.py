"""The DAEMON Score v2 adversarial case library.

Cases cover the known failure modes for a performance-scoring model: tanks,
supports, split pushers, weak-side play, short games, low-KDA-but-influential
performances, vision without conversion, raw economy without influence,
objective contact without a real contest, and disputed/ambiguous scores.
Two cases are ``verified_local`` -- grounded in real local aggregate rows
and, where recorded in ``evidence_provenance``, an authoritative LCU match
details/timeline response observed during the original investigation. All
evidence is sanitized to remove PUUID/summoner name. The rest are
``synthetic`` and explicitly labeled as such; neither this module nor its
data file ever claims a synthetic case is real evidence.

The case data itself lives in ``corpus/data/adversarial_cases.json`` so it
can be reviewed/edited without touching code, but all structural validation
(unique ids, known categories, an expectation shape ``evaluate_case`` can
actually interpret, and the same credential/identifier privacy scan used by
``manifest.py``) happens here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from corpus._privacy import scan_for_forbidden

_DATA_PATH = Path(__file__).resolve().parent / "data" / "adversarial_cases.json"

CATEGORIES = (
    "tank",
    "support",
    "split_pusher",
    "weak_side_play",
    "short_game",
    "low_kda_influence",
    "vision_without_conversion",
    "raw_economy_without_influence",
    "objective_contact_without_contest",
    "disputed_score",
)

VERIFICATION_STATUSES = ("verified_local", "synthetic")

EXPECTATION_TYPES = (
    "insufficient_evidence",
    "pairwise_minimum_gap",
    "must_not_rank_first_solely_on_metric",
    "disputed_manual_review",
    "compound",
)


class AdversarialCaseError(Exception):
    """Raised when the case library or a single case fails validation."""


@dataclass(frozen=True)
class AdversarialCase:
    case_id: str
    category: str
    title: str
    description: str
    verification_status: str
    game_id: Optional[int]
    evidence_provenance: Mapping
    sanitized_facts: Mapping
    expectation: Mapping
    rationale: str
    tags: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "title": self.title,
            "description": self.description,
            "verification_status": self.verification_status,
            "game_id": self.game_id,
            "evidence_provenance": self.evidence_provenance,
            "sanitized_facts": self.sanitized_facts,
            "expectation": self.expectation,
            "rationale": self.rationale,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "AdversarialCase":
        return cls(
            case_id=data["case_id"],
            category=data["category"],
            title=data["title"],
            description=data["description"],
            verification_status=data["verification_status"],
            game_id=data.get("game_id"),
            evidence_provenance=data.get("evidence_provenance", {}),
            sanitized_facts=data.get("sanitized_facts", {}),
            expectation=data["expectation"],
            rationale=data.get("rationale", ""),
            tags=tuple(data.get("tags", ())),
        )


def load_library(path: Optional[Path] = None) -> list[AdversarialCase]:
    raw = json.loads(Path(path or _DATA_PATH).read_text(encoding="utf-8"))
    try:
        cases = [AdversarialCase.from_dict(item) for item in raw["cases"]]
    except (KeyError, TypeError) as exc:
        raise AdversarialCaseError(
            f"Malformed adversarial case entry: missing or invalid field {exc}"
        ) from exc
    validate_library(cases)
    return cases


def validate_library(cases: Sequence[AdversarialCase]) -> None:
    seen_ids: set[str] = set()
    for case in cases:
        if case.case_id in seen_ids:
            raise AdversarialCaseError(f"Duplicate case_id '{case.case_id}'")
        seen_ids.add(case.case_id)
        if case.category not in CATEGORIES:
            raise AdversarialCaseError(
                f"Case '{case.case_id}' has unknown category '{case.category}'"
            )
        if case.verification_status not in VERIFICATION_STATUSES:
            raise AdversarialCaseError(
                f"Case '{case.case_id}' has unknown verification_status "
                f"'{case.verification_status}'"
            )
        if case.verification_status == "verified_local" and not case.game_id:
            raise AdversarialCaseError(
                f"Verified case '{case.case_id}' must carry a real game_id"
            )
        if (
            case.verification_status == "verified_local"
            and not case.evidence_provenance
        ):
            raise AdversarialCaseError(
                f"Verified case '{case.case_id}' must document evidence_provenance"
            )
        _validate_expectation(case.case_id, case.expectation)
        problems = scan_for_forbidden(case.to_dict())
        if problems:
            raise AdversarialCaseError(
                f"Case '{case.case_id}' contains disallowed data: "
                + "; ".join(problems)
            )


def _validate_expectation(case_id: str, expectation: Mapping) -> None:
    expectation_type = expectation.get("type")
    if expectation_type not in EXPECTATION_TYPES:
        raise AdversarialCaseError(
            f"Case '{case_id}' has unknown expectation type '{expectation_type}'"
        )
    if expectation_type == "pairwise_minimum_gap":
        for key in ("winner", "loser", "min_gap"):
            if key not in expectation:
                raise AdversarialCaseError(
                    f"Case '{case_id}' pairwise_minimum_gap expectation "
                    f"missing '{key}'"
                )
    elif expectation_type == "must_not_rank_first_solely_on_metric":
        for key in ("subject", "metric"):
            if key not in expectation:
                raise AdversarialCaseError(
                    f"Case '{case_id}' must_not_rank_first_solely_on_metric "
                    f"expectation missing '{key}'"
                )
    elif expectation_type == "insufficient_evidence":
        if "duration_threshold_seconds" not in expectation:
            raise AdversarialCaseError(
                f"Case '{case_id}' insufficient_evidence expectation "
                "missing 'duration_threshold_seconds'"
            )
    elif expectation_type == "compound":
        sub_expectations = expectation.get("sub_expectations")
        if not sub_expectations:
            raise AdversarialCaseError(
                f"Case '{case_id}' compound expectation missing "
                "'sub_expectations'"
            )
        for sub in sub_expectations:
            _validate_expectation(case_id, sub)


@dataclass(frozen=True)
class EvaluationResult:
    passed: Optional[bool]
    detail: str
    sub_results: tuple["EvaluationResult", ...] = ()

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "detail": self.detail,
            "sub_results": [sub.to_dict() for sub in self.sub_results],
        }


def evaluate_case(
        case: AdversarialCase, *, scores: Optional[Mapping[str, float]] = None,
        duration_seconds: Optional[int] = None) -> EvaluationResult:
    """Check a candidate model's output against one case's expectation.

    ``scores`` maps a subject label (as used in the case's
    ``sanitized_facts``/``expectation``, e.g. a champion name) to a
    candidate numeric score. This function is deliberately decoupled from
    any concrete scoring engine -- callers pass in whatever scores they want
    checked, whether from a real model run or a hand-built test fixture.
    """
    return _evaluate_expectation(case.expectation, scores=scores, duration_seconds=duration_seconds)


def _evaluate_expectation(
        expectation: Mapping, *, scores: Optional[Mapping[str, float]],
        duration_seconds: Optional[int]) -> EvaluationResult:
    expectation_type = expectation["type"]

    if expectation_type == "disputed_manual_review":
        return EvaluationResult(
            passed=None,
            detail="This case requires manual review; no automatic verdict is asserted.",
        )

    if expectation_type == "insufficient_evidence":
        if duration_seconds is None:
            return EvaluationResult(
                passed=None, detail="duration_seconds was not supplied.",
            )
        threshold = expectation["duration_threshold_seconds"]
        passed = duration_seconds < threshold
        return EvaluationResult(
            passed=passed,
            detail=(
                f"duration_seconds={duration_seconds} "
                f"{'<' if passed else '>='} threshold={threshold}"
            ),
        )

    if expectation_type == "pairwise_minimum_gap":
        if scores is None:
            return EvaluationResult(passed=None, detail="scores were not supplied.")
        winner, loser = expectation["winner"], expectation["loser"]
        if winner not in scores or loser not in scores:
            return EvaluationResult(
                passed=None,
                detail=f"scores missing '{winner}' or '{loser}'.",
            )
        gap = scores[winner] - scores[loser]
        passed = gap >= expectation["min_gap"]
        return EvaluationResult(
            passed=passed,
            detail=(
                f"{winner}({scores[winner]}) - {loser}({scores[loser]}) = "
                f"{gap} {'>=' if passed else '<'} min_gap={expectation['min_gap']}"
            ),
        )

    if expectation_type == "must_not_rank_first_solely_on_metric":
        if scores is None:
            return EvaluationResult(passed=None, detail="scores were not supplied.")
        subject = expectation["subject"]
        if subject not in scores:
            return EvaluationResult(
                passed=None, detail=f"scores missing '{subject}'.",
            )
        top_scorer = max(scores, key=lambda key: scores[key])
        passed = top_scorer != subject
        return EvaluationResult(
            passed=passed,
            detail=(
                f"top scorer is '{top_scorer}' "
                f"({'not' if passed else 'is'} '{subject}')"
            ),
        )

    if expectation_type == "compound":
        sub_results = tuple(
            _evaluate_expectation(sub, scores=scores, duration_seconds=duration_seconds)
            for sub in expectation["sub_expectations"]
        )
        outcomes = [result.passed for result in sub_results]
        if False in outcomes:
            overall: Optional[bool] = False
        elif None in outcomes:
            overall = None
        else:
            overall = True
        resolved = [outcome for outcome in outcomes if outcome is not None]
        return EvaluationResult(
            passed=overall,
            detail=f"{len(resolved)}/{len(sub_results)} sub-expectations resolved",
            sub_results=sub_results,
        )

    raise AdversarialCaseError(f"Unhandled expectation type '{expectation_type}'")


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Adversarial case library utilities")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="Load and validate the bundled case library")
    list_parser = sub.add_parser("list", help="List all cases by category")
    list_parser.add_argument("--category")

    args = parser.parse_args()
    if args.command == "validate":
        try:
            cases = load_library()
        except AdversarialCaseError as exc:
            print(f"INVALID: {exc}")
            return 1
        print(f"OK: {len(cases)} cases across {len(CATEGORIES)} categories")
        return 0
    if args.command == "list":
        cases = load_library()
        for case in cases:
            if args.category and case.category != args.category:
                continue
            print(f"{case.case_id}\t{case.category}\t{case.verification_status}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
