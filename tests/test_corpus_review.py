"""Tests for corpus.review: blinding, append-only labels, agreement,
export."""

import datetime
import json

import pytest

from corpus.review import (
    ALLOWED_PRESENTATION_FIELDS,
    PairwiseItem,
    ReviewLabelStore,
    ReviewValidationError,
    build_presentation,
    compute_agreement,
    export_for_training,
    make_label,
    make_pair_id,
    redact_for_presentation,
    validate_label,
)


def _item(ref, **features):
    return PairwiseItem(item_ref=ref, features=features)


def test_redact_for_presentation_strips_disallowed_fields():
    record = {
        "champion_name": "K'Sante", "role": "TOP", "kills": 8,
        "puuid": "should-not-leak", "summoner_name": "RealName",
        "win": True, "total_score": 52.5, "match_rank": 6,
        "team_id": 200, "participant_id": 8, "game_id": 5602827182,
    }
    redacted = redact_for_presentation(record)
    assert set(redacted.keys()) <= set(ALLOWED_PRESENTATION_FIELDS)
    for forbidden in (
            "puuid", "summoner_name", "win", "total_score", "match_rank",
            "team_id", "participant_id", "game_id"):
        assert forbidden not in redacted


def test_redact_for_presentation_keeps_allowed_gameplay_fields():
    record = {"champion_name": "Ahri", "kills": 5, "deaths": 2, "assists": 7}
    redacted = redact_for_presentation(record)
    assert redacted == record


def test_make_pair_id_is_order_independent():
    item_a = _item("game1:1", champion_name="Ahri")
    item_b = _item("game1:2", champion_name="Zed")
    a = make_pair_id(item_a, item_b)
    b = make_pair_id(item_b, item_a)
    assert a == b


def test_make_pair_id_differs_for_different_pairs():
    item_a = _item("game1:1", champion_name="Ahri")
    item_b = _item("game1:2", champion_name="Zed")
    item_c = _item("game1:3", champion_name="Garen")
    a = make_pair_id(item_a, item_b)
    b = make_pair_id(item_a, item_c)
    assert a != b


def test_build_presentation_never_leaks_forbidden_fields():
    item_a = _item("g:1", champion_name="K'Sante", kills=8, deaths=7, assists=4,
                    puuid="raw-puuid-a", win=True, total_score=52.5)
    item_b = _item("g:2", champion_name="Seraphine", kills=3, deaths=15, assists=14,
                    summoner_name="RealName", win=False, total_score=55.0)
    presentation, token_map = build_presentation(item_a, item_b, seed="s1")

    for view in (presentation.left_view, presentation.right_view):
        assert "puuid" not in view
        assert "summoner_name" not in view
        assert "win" not in view
        assert "total_score" not in view

    # Real refs are only reachable via the private token_map, never via the
    # presentation object itself.
    assert "g:1" not in json.dumps(presentation.to_dict())
    assert "g:2" not in json.dumps(presentation.to_dict())
    assert {token_map["left_ref"], token_map["right_ref"]} == {"g:1", "g:2"}


def test_build_presentation_is_deterministic_given_same_seed():
    item_a = _item("g:1", champion_name="Ahri", kills=1)
    item_b = _item("g:2", champion_name="Zed", kills=2)
    p1, tm1 = build_presentation(item_a, item_b, seed="fixed-seed")
    p2, tm2 = build_presentation(item_a, item_b, seed="fixed-seed")
    assert p1.pair_id == p2.pair_id
    assert p1.left_token == p2.left_token
    assert p1.right_token == p2.right_token
    assert tm1 == tm2


def test_build_presentation_order_can_change_with_different_seed():
    item_a = _item("g:1", champion_name="Ahri", kills=1)
    item_b = _item("g:2", champion_name="Zed", kills=2)
    orders = set()
    for seed in ("s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"):
        presentation, token_map = build_presentation(item_a, item_b, seed=seed)
        orders.add(token_map["left_ref"])
    # With 8 different seeds we should see both possible left assignments
    # at least once (extremely unlikely to fail if randomization works).
    assert orders == {"g:1", "g:2"}


def test_make_label_validates_choice():
    with pytest.raises(ReviewValidationError):
        make_label(pair_id="abc", reviewer_id="rev1", choice="not_a_choice",
                   confidence=0.5, rationale_tags=("combat_impact",))


def test_make_label_validates_confidence_range():
    with pytest.raises(ReviewValidationError):
        make_label(pair_id="abc", reviewer_id="rev1", choice="left",
                   confidence=5, rationale_tags=("combat_impact",))


def test_make_label_validates_rationale_tags():
    with pytest.raises(ReviewValidationError):
        make_label(pair_id="abc", reviewer_id="rev1", choice="left",
                   confidence=0.5, rationale_tags=("not_a_real_tag",))


def test_make_label_requires_reviewer_id():
    with pytest.raises(ReviewValidationError):
        make_label(pair_id="abc", reviewer_id="", choice="left",
                   confidence=0.5, rationale_tags=("combat_impact",))


def test_make_label_accepts_tie_and_insufficient_evidence_choices():
    tie = make_label(pair_id="abc", reviewer_id="rev1", choice="tie",
                      confidence=0.9, rationale_tags=("role_context",))
    insufficient = make_label(pair_id="abc", reviewer_id="rev1",
                               choice="insufficient_evidence", confidence=0.2,
                               rationale_tags=("insufficient_data",))
    assert validate_label(tie) is None
    assert validate_label(insufficient) is None


def test_make_label_rejects_credential_shaped_notes():
    with pytest.raises(ReviewValidationError):
        make_label(pair_id="abc", reviewer_id="rev1", choice="left",
                   confidence=0.5, rationale_tags=("combat_impact",),
                   notes="RGAPI-12345678-1234-1234-1234-123456789012")


def test_label_store_is_append_only(tmp_path):
    store = ReviewLabelStore(tmp_path / "labels.jsonl")
    assert not hasattr(store, "update_label")
    assert not hasattr(store, "delete_label")
    label = make_label(pair_id="p1", reviewer_id="rev1", choice="left",
                        confidence=0.7, rationale_tags=("combat_impact",))
    store.add_label(label)
    label2 = make_label(pair_id="p1", reviewer_id="rev1", choice="right",
                         confidence=0.6, rationale_tags=("role_context",))
    store.add_label(label2)  # a "correction" is a new row, not a mutation
    labels = list(store.iter_labels())
    assert len(labels) == 2


def test_label_store_persists_across_instances(tmp_path):
    path = tmp_path / "labels.jsonl"
    store1 = ReviewLabelStore(path)
    store1.add_label(make_label(pair_id="p1", reviewer_id="rev1", choice="left",
                                 confidence=0.7, rationale_tags=("combat_impact",)))
    store2 = ReviewLabelStore(path)
    labels = list(store2.iter_labels())
    assert len(labels) == 1


def test_label_store_skips_malformed_lines_without_crashing(tmp_path):
    path = tmp_path / "labels.jsonl"
    store = ReviewLabelStore(path)
    store.add_label(make_label(pair_id="p1", reviewer_id="rev1", choice="left",
                                confidence=0.7, rationale_tags=("combat_impact",)))
    with open(path, "a", encoding="utf-8") as f:
        f.write("not valid json at all\n")
        f.write("\n")  # blank line should also be tolerated
    labels = list(store.iter_labels())
    assert len(labels) == 1
    assert store.last_load_errors


def test_compute_agreement_unanimous_pair():
    labels = [
        make_label(pair_id="p1", reviewer_id="rev1", choice="left",
                   confidence=0.8, rationale_tags=("combat_impact",)),
        make_label(pair_id="p1", reviewer_id="rev2", choice="left",
                   confidence=0.9, rationale_tags=("combat_impact",)),
    ]
    report = compute_agreement(labels)
    assert report.agreement_rate == pytest.approx(1.0)
    assert report.pairwise_cohens_kappa == pytest.approx(1.0)


def test_compute_agreement_disagreement_pair():
    labels = [
        make_label(pair_id="p1", reviewer_id="rev1", choice="left",
                   confidence=0.8, rationale_tags=("combat_impact",)),
        make_label(pair_id="p1", reviewer_id="rev2", choice="right",
                   confidence=0.9, rationale_tags=("combat_impact",)),
    ]
    report = compute_agreement(labels)
    assert report.agreement_rate == pytest.approx(0.0)


def test_compute_agreement_kappa_is_none_with_more_than_two_reviewers():
    labels = [
        make_label(pair_id="p1", reviewer_id="rev1", choice="left",
                   confidence=0.8, rationale_tags=("combat_impact",)),
        make_label(pair_id="p1", reviewer_id="rev2", choice="left",
                   confidence=0.9, rationale_tags=("combat_impact",)),
        make_label(pair_id="p1", reviewer_id="rev3", choice="right",
                   confidence=0.5, rationale_tags=("combat_impact",)),
    ]
    report = compute_agreement(labels)
    assert report.pairwise_cohens_kappa is None
    assert report.agreement_rate is not None  # percent-agreement still works


def test_compute_agreement_kappa_is_none_with_single_reviewer():
    labels = [
        make_label(pair_id="p1", reviewer_id="rev1", choice="left",
                   confidence=0.8, rationale_tags=("combat_impact",)),
    ]
    report = compute_agreement(labels)
    assert report.pairwise_cohens_kappa is None


def test_export_for_training_resolves_refs_via_token_map():
    item_a = _item("g:1", champion_name="Ahri", kills=1)
    item_b = _item("g:2", champion_name="Zed", kills=2)
    presentation, token_map = build_presentation(item_a, item_b, seed="export-seed")
    label = make_label(pair_id=presentation.pair_id, reviewer_id="rev1",
                        choice="left", confidence=0.8,
                        rationale_tags=("combat_impact",))
    rows = export_for_training([label], {presentation.pair_id: token_map})
    assert len(rows) == 1
    row = rows[0]
    assert row["winner_ref"] == token_map["left_ref"]
    assert row["relation"] == "left_preferred"


def test_export_for_training_uses_latest_append_only_correction():
    item_a = _item("g:1", champion_name="Ahri", kills=1)
    item_b = _item("g:2", champion_name="Zed", kills=2)
    presentation, token_map = build_presentation(
        item_a, item_b, seed="correction-seed",
    )
    original = make_label(
        pair_id=presentation.pair_id, reviewer_id="rev1",
        choice="left", confidence=0.6,
        rationale_tags=("combat_impact",),
        now=datetime.datetime.fromisoformat("2026-07-14T01:00:00+00:00"),
    )
    correction = make_label(
        pair_id=presentation.pair_id, reviewer_id="rev1",
        choice="right", confidence=0.9,
        rationale_tags=("economy_efficiency",),
        now=datetime.datetime.fromisoformat("2026-07-14T01:05:00+00:00"),
    )

    rows = export_for_training(
        [original, correction], {presentation.pair_id: token_map},
    )

    assert len(rows) == 1
    assert rows[0]["choice"] == "right"
    assert rows[0]["winner_ref"] == token_map["right_ref"]
    assert rows[0]["confidence"] == 0.9


def test_export_for_training_raises_on_missing_mapping_by_default():
    label = make_label(pair_id="unknown-pair", reviewer_id="rev1", choice="left",
                        confidence=0.8, rationale_tags=("combat_impact",))
    with pytest.raises(ReviewValidationError):
        export_for_training([label], {})


def test_export_for_training_can_skip_missing_mapping():
    label = make_label(pair_id="unknown-pair", reviewer_id="rev1", choice="left",
                        confidence=0.8, rationale_tags=("combat_impact",))
    rows = export_for_training([label], {}, on_missing_mapping="skip")
    assert rows == []


def test_export_for_training_handles_tie_and_insufficient_evidence():
    item_a = _item("g:1", champion_name="Ahri", kills=1)
    item_b = _item("g:2", champion_name="Zed", kills=2)
    presentation, token_map = build_presentation(item_a, item_b, seed="tie-seed")
    tie_label = make_label(pair_id=presentation.pair_id, reviewer_id="rev1",
                            choice="tie", confidence=0.5,
                            rationale_tags=("role_context",))
    rows = export_for_training([tie_label], {presentation.pair_id: token_map})
    assert rows[0]["winner_ref"] is None
    assert rows[0]["relation"] == "tie"
