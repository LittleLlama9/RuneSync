"""Tests for corpus.adversarial_cases: library loading/validation and case
evaluation, including the two verified real-game cases."""

import json

import pytest

from corpus.adversarial_cases import (
    CATEGORIES,
    AdversarialCaseError,
    evaluate_case,
    load_library,
    validate_library,
)


def test_library_loads_without_error():
    cases = load_library()
    assert len(cases) > 0


def test_library_covers_all_required_categories():
    cases = load_library()
    covered = {c.category for c in cases}
    assert covered == set(CATEGORIES)


def test_library_has_unique_case_ids():
    cases = load_library()
    ids = [c.case_id for c in cases]
    assert len(ids) == len(set(ids))


def test_two_verified_local_cases_present_with_correct_game_ids():
    cases = load_library()
    verified = {c.case_id: c for c in cases if c.verification_status == "verified_local"}
    assert len(verified) == 2

    short_game_case = next(
        c for c in verified.values() if c.category == "short_game"
    )
    assert short_game_case.game_id == 5601631110

    disputed_case = next(
        c for c in verified.values() if c.category == "disputed_score"
    )
    assert disputed_case.game_id == 5602827182
    assert all(case.evidence_provenance for case in verified.values())


def test_ksante_case_preserves_authoritative_timeline_provenance():
    case = _disputed_case()
    sources = {
        source["kind"]: source
        for source in case.evidence_provenance["sources"]
    }
    timeline = sources["authoritative_lcu_timeline"]
    facts = timeline["observed_facts"]

    assert timeline["frame_count"] == 33
    assert timeline["last_frame_seconds"] == 1861
    assert len(facts["ksante_grub_events"]) == 2
    assert len(facts["ksante_turret_kills"]) == 2
    assert max(
        point["gold_diff"] for point in facts["ksante_gold_lead_vs_yone"]
    ) == 2143
    assert len(facts["velkoz_direct_map_events"]) == 1
    assert facts["seraphine_direct_map_event_count"] == 0


def test_validate_library_accepts_bundled_library():
    cases = load_library()
    validate_library(cases)  # should not raise


def test_validate_library_rejects_duplicate_case_ids():
    cases = load_library()
    dupe = cases[0]
    with pytest.raises(AdversarialCaseError):
        validate_library(list(cases) + [dupe])


def test_validate_library_rejects_unknown_category(tmp_path):
    bad_path = tmp_path / "bad_cases.json"
    bad_path.write_text(json.dumps({
        "schema_version": 1,
        "cases": [{
            "case_id": "bad-case",
            "category": "not_a_real_category",
            "title": "Bad case",
            "verification_status": "synthetic",
            "description": "x",
            "expectation": {"type": "insufficient_evidence", "threshold_seconds": 1},
        }],
    }), encoding="utf-8")
    with pytest.raises(AdversarialCaseError):
        load_library(bad_path)


def test_validate_library_requires_game_id_for_verified_cases(tmp_path):
    bad_path = tmp_path / "bad_cases.json"
    bad_path.write_text(json.dumps({
        "schema_version": 1,
        "cases": [{
            "case_id": "bad-verified-case",
            "category": "short_game",
            "title": "Bad verified case",
            "verification_status": "verified_local",
            "description": "x",
            "expectation": {"type": "insufficient_evidence", "threshold_seconds": 900},
        }],
    }), encoding="utf-8")
    with pytest.raises(AdversarialCaseError):
        load_library(bad_path)


def test_validate_library_rejects_forbidden_data(tmp_path):
    bad_path = tmp_path / "bad_cases.json"
    bad_path.write_text(json.dumps({
        "schema_version": 1,
        "cases": [{
            "case_id": "bad-credential-case",
            "category": "tank",
            "title": "Bad credential case",
            "verification_status": "synthetic",
            "description": "RGAPI-12345678-1234-1234-1234-123456789012",
            "expectation": {"type": "insufficient_evidence", "threshold_seconds": 900},
        }],
    }), encoding="utf-8")
    with pytest.raises(AdversarialCaseError):
        load_library(bad_path)


def test_load_library_rejects_malformed_json(tmp_path):
    bad_path = tmp_path / "malformed.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises((AdversarialCaseError, json.JSONDecodeError)):
        load_library(bad_path)


# --- evaluate_case() behavior for the two verified cases ---


def _short_game_case():
    cases = load_library()
    return next(
        c for c in cases
        if c.verification_status == "verified_local" and c.category == "short_game"
    )


def _disputed_case():
    cases = load_library()
    return next(
        c for c in cases
        if c.verification_status == "verified_local" and c.category == "disputed_score"
    )


def test_short_game_case_passes_when_duration_below_threshold():
    case = _short_game_case()
    result = evaluate_case(case, duration_seconds=510)  # real 8:30 duration
    assert result.passed is True


def test_short_game_case_fails_when_duration_is_a_normal_length_game():
    case = _short_game_case()
    result = evaluate_case(case, duration_seconds=1861)
    assert result.passed is False


def test_short_game_case_cannot_resolve_without_duration_data():
    case = _short_game_case()
    result = evaluate_case(case)
    assert result.passed is None


def test_disputed_case_passes_when_ksante_beats_seraphine_and_velkoz():
    case = _disputed_case()
    scores = {"K'Sante": 60.0, "Seraphine": 55.0, "Vel'Koz": 54.0}
    result = evaluate_case(case, scores=scores)
    assert result.passed is True
    assert all(sub.passed for sub in result.sub_results)


def test_disputed_case_fails_when_ksante_ranked_below_seraphine():
    case = _disputed_case()
    # Real v1 scores from the local DB: K'Sante 52.5 (rank 6), Seraphine 55.0
    # (rank 3) -- this is the exact adversarial failure the case must catch.
    scores = {"K'Sante": 52.5, "Seraphine": 55.0, "Vel'Koz": 54.2}
    result = evaluate_case(case, scores=scores)
    assert result.passed is False


def test_disputed_case_fails_when_velkoz_outranks_ksante():
    case = _disputed_case()
    scores = {"K'Sante": 58.0, "Seraphine": 50.0, "Vel'Koz": 74.6}
    result = evaluate_case(case, scores=scores)
    assert result.passed is False
    # The K'Sante-over-Vel'Koz sub-expectation specifically must fail.
    velkoz_failures = [
        sub for sub in result.sub_results
        if sub.passed is False
    ]
    assert velkoz_failures


def test_disputed_case_cannot_resolve_without_scores():
    case = _disputed_case()
    result = evaluate_case(case)
    assert result.passed is None


def test_compound_case_stays_unresolved_when_one_comparison_is_missing():
    case = _disputed_case()
    result = evaluate_case(
        case, scores={"K'Sante": 60.0, "Vel'Koz": 55.0},
    )

    assert result.passed is None


def test_evaluate_case_handles_disputed_manual_review_type():
    cases = load_library()
    disputed_manual = next(
        c for c in cases if c.expectation.get("type") == "disputed_manual_review"
    )
    result = evaluate_case(disputed_manual, scores={"Anything": 1.0})
    assert result.passed is None
