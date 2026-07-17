"""Tests for score_v2/training/dataset.py -- the documented dataset schema.

Sections:
  1. FeatureRecord construction/validation (leakage rejection, base_ref/
     item_ref consistency, split validation).
  2. Multi-tier record identity: the same game/participant with both
     `aggregate` and `lcu_timeline` (etc.) records coexisting.
  3. PairLabel validation (enum choice, relation/winner_ref consistency,
     confidence range, distinct refs, unknown choice rejection).
  4. TrainingDataset dedup/reference validation (including duplicate
     pair-label detection).
  5. JSONL save/load round trip, deterministic ordering, and header
     count/tamper detection.
  6. `select_split` -- never falls back to "everything".
  7. StateValueLabel isolation from FeatureRecord/feature extraction.
  8. CLI `validate` subcommand.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from score_v2.leakage import OutcomeLeakageError
from score_v2.training.dataset import (
    DatasetValidationError,
    FeatureRecord,
    PairLabel,
    StateValueLabel,
    TrainingDataset,
    build_feature_record,
    build_state_value_labels_from_match,
    load_state_value_labels_jsonl,
    make_base_ref,
    make_item_ref,
    parse_base_ref,
    save_state_value_labels_jsonl,
    select_split,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _game_features(participants, **overrides):
    payload = {
        "duration_seconds": 1800.0, "abstain": False, "abstain_reason": None,
        "chosen_source_completeness": 1.0, "participants": participants,
    }
    payload.update(overrides)
    return payload


def _block(role="mid"):
    return {
        "raw": {"kills": 5, "deaths": 3, "assists": 4},
        "baseline": {"role": role, "champion": "TestChamp", "patch": "14.1"},
    }


def _pair(left_ref, right_ref, choice="left", **overrides):
    if choice == "left":
        winner_ref, relation = left_ref, "left_preferred"
    elif choice == "right":
        winner_ref, relation = right_ref, "right_preferred"
    else:
        winner_ref, relation = None, choice
    kwargs = dict(
        pair_id=f"{left_ref}|{right_ref}", left_ref=left_ref, right_ref=right_ref,
        winner_ref=winner_ref, relation=relation, choice=choice, confidence=0.8,
        rationale_tags=("combat_impact",), reviewer_id="r1",
        created_at="2026-01-01T00:00:00+00:00",
    )
    kwargs.update(overrides)
    return PairLabel(**kwargs)


# ── 1. FeatureRecord ─────────────────────────────────────────────────────────

def test_build_feature_record_basic():
    gf = _game_features({"1": _block()})
    record = build_feature_record(
        game_id=111, participant_id=1, evidence_source="match_v5", features_for_game=gf,
    )
    assert record.item_ref == "111:1:match_v5"
    assert record.base_ref == "111:1"
    assert record.role == "mid"
    assert record.evidence_source == "match_v5"
    assert record.duration_seconds == 1800.0


def test_build_feature_record_missing_participant_raises():
    gf = _game_features({"1": _block()})
    with pytest.raises(DatasetValidationError):
        build_feature_record(
            game_id=111, participant_id=2, evidence_source="match_v5", features_for_game=gf,
        )


def test_feature_record_rejects_outcome_leakage():
    block = _block()
    block["local_win"] = True
    gf = _game_features({"1": block})
    with pytest.raises(OutcomeLeakageError):
        build_feature_record(
            game_id=111, participant_id=1, evidence_source="match_v5", features_for_game=gf,
        )


def test_feature_record_rejects_mismatched_base_ref():
    with pytest.raises(DatasetValidationError):
        FeatureRecord(
            item_ref="111:1:match_v5", base_ref="999:1", game_id=111, participant_id=1,
            evidence_source="match_v5", role="mid", duration_seconds=1800.0, abstain=False,
            abstain_reason=None, chosen_source_completeness=1.0, features=_block(),
            split="train",
        )


def test_feature_record_rejects_mismatched_item_ref():
    with pytest.raises(DatasetValidationError):
        FeatureRecord(
            item_ref="999:1:match_v5", base_ref="111:1", game_id=111, participant_id=1,
            evidence_source="match_v5", role="mid", duration_seconds=1800.0, abstain=False,
            abstain_reason=None, chosen_source_completeness=1.0, features=_block(),
            split="train",
        )


def test_feature_record_rejects_unknown_split():
    with pytest.raises(DatasetValidationError):
        FeatureRecord(
            item_ref="111:1:match_v5", base_ref="111:1", game_id=111, participant_id=1,
            evidence_source="match_v5", role="mid", duration_seconds=1800.0, abstain=False,
            abstain_reason=None, chosen_source_completeness=1.0, features=_block(),
            split="bogus_split",
        )


def test_feature_record_to_dict_from_dict_round_trip():
    gf = _game_features({"1": _block()})
    record = build_feature_record(
        game_id=111, participant_id=1, evidence_source="aggregate", features_for_game=gf,
        split="train",
    )
    restored = FeatureRecord.from_dict(record.to_dict())
    assert restored == record


def test_make_base_ref_and_parse_round_trip():
    ref = make_base_ref(555, 7)
    assert ref == "555:7"
    assert parse_base_ref(ref) == (555, 7)


def test_make_item_ref_is_tier_specific():
    assert make_item_ref(555, 7, "match_v5") == "555:7:match_v5"
    assert make_item_ref(555, 7, "aggregate") == "555:7:aggregate"
    assert make_item_ref(555, 7, "match_v5") != make_item_ref(555, 7, "aggregate")


# ── 2. multi-tier record identity ───────────────────────────────────────────

def _record(game_id, participant_id, evidence_source="match_v5", split="train"):
    gf = _game_features({str(participant_id): _block()})
    return build_feature_record(
        game_id=game_id, participant_id=participant_id, evidence_source=evidence_source,
        features_for_game=gf, split=split,
    )


def test_same_game_participant_can_have_records_in_multiple_tiers():
    """The normal case: aggregate + lcu_timeline (+ match_v5/live_client)
    evidence for the same game/participant coexisting without collision.
    """
    aggregate_record = _record(1, 1, evidence_source="aggregate")
    lcu_record = _record(1, 1, evidence_source="lcu_timeline")
    match_v5_record = _record(1, 1, evidence_source="match_v5")

    assert aggregate_record.base_ref == lcu_record.base_ref == match_v5_record.base_ref == "1:1"
    assert len({aggregate_record.item_ref, lcu_record.item_ref, match_v5_record.item_ref}) == 3

    dataset = TrainingDataset(
        schema_version=1,
        feature_records=(aggregate_record, lcu_record, match_v5_record),
        pair_labels=(),
    )
    dataset.validate()  # must NOT raise a duplicate/collision error
    assert len(dataset.feature_records_by_ref()) == 3


def test_feature_records_by_base_ref_rejects_mixed_tier_dataset():
    aggregate_record = _record(1, 1, evidence_source="aggregate")
    lcu_record = _record(1, 1, evidence_source="lcu_timeline")
    dataset = TrainingDataset(
        schema_version=1, feature_records=(aggregate_record, lcu_record), pair_labels=(),
    )
    with pytest.raises(DatasetValidationError):
        dataset.feature_records_by_base_ref()


def test_feature_records_by_base_ref_works_for_single_tier_dataset():
    r1, r2 = _record(1, 1, evidence_source="match_v5"), _record(1, 2, evidence_source="match_v5")
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=())
    by_base_ref = dataset.feature_records_by_base_ref()
    assert set(by_base_ref) == {"1:1", "1:2"}


# ── 3. PairLabel validation ──────────────────────────────────────────────────

def test_pair_label_rejects_unknown_choice():
    with pytest.raises(DatasetValidationError):
        _pair("1:1", "1:2", choice="maybe", relation="maybe", winner_ref=None)


def test_pair_label_rejects_identical_refs():
    with pytest.raises(DatasetValidationError):
        _pair("1:1", "1:1", choice="left")


def test_pair_label_rejects_confidence_out_of_range():
    with pytest.raises(DatasetValidationError):
        _pair("1:1", "1:2", choice="left", confidence=1.5)
    with pytest.raises(DatasetValidationError):
        _pair("1:1", "1:2", choice="left", confidence=-0.1)


def test_pair_label_rejects_left_choice_with_wrong_relation():
    with pytest.raises(DatasetValidationError):
        _pair("1:1", "1:2", choice="left", relation="right_preferred")


def test_pair_label_rejects_left_choice_with_wrong_winner_ref():
    with pytest.raises(DatasetValidationError):
        _pair("1:1", "1:2", choice="left", winner_ref="1:2")


def test_pair_label_rejects_tie_with_nonnull_winner_ref():
    with pytest.raises(DatasetValidationError):
        _pair("1:1", "1:2", choice="tie", winner_ref="1:1")


def test_pair_label_rejects_tie_with_wrong_relation():
    with pytest.raises(DatasetValidationError):
        _pair("1:1", "1:2", choice="tie", relation="not_tie")


def test_pair_label_accepts_valid_left_right_tie_insufficient():
    _pair("1:1", "1:2", choice="left")
    _pair("1:1", "1:2", choice="right")
    _pair("1:1", "1:2", choice="tie")
    _pair("1:1", "1:2", choice="insufficient_evidence")


def test_pair_label_rejects_empty_reviewer_id():
    with pytest.raises(DatasetValidationError):
        _pair("1:1", "1:2", choice="left", reviewer_id="")


# ── 4. TrainingDataset validation ────────────────────────────────────────────

def test_dataset_rejects_duplicate_item_ref():
    r1 = _record(1, 1)
    r2 = _record(1, 1)
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=())
    with pytest.raises(DatasetValidationError):
        dataset.validate()


def test_dataset_rejects_pair_referencing_unknown_base_ref():
    r1 = _record(1, 1)
    pair = _pair("1:1", "999:1", choice="left")
    dataset = TrainingDataset(schema_version=1, feature_records=(r1,), pair_labels=(pair,))
    with pytest.raises(DatasetValidationError):
        dataset.validate()


def test_dataset_valid_with_matching_pair():
    r1, r2 = _record(1, 1), _record(1, 2)
    pair = _pair("1:1", "1:2", choice="left")
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=(pair,))
    dataset.validate()  # should not raise


def test_dataset_rejects_duplicate_pair_same_reviewer():
    r1, r2 = _record(1, 1), _record(1, 2)
    pair_a = _pair("1:1", "1:2", choice="left", pair_id="dup", reviewer_id="r1")
    pair_b = _pair("1:1", "1:2", choice="right", pair_id="dup", reviewer_id="r1")
    dataset = TrainingDataset(
        schema_version=1, feature_records=(r1, r2), pair_labels=(pair_a, pair_b),
    )
    with pytest.raises(DatasetValidationError):
        dataset.validate()


def test_dataset_allows_same_pair_from_distinct_reviewers():
    r1, r2 = _record(1, 1), _record(1, 2)
    pair_a = _pair("1:1", "1:2", choice="left", pair_id="dup", reviewer_id="r1")
    pair_b = _pair("1:1", "1:2", choice="left", pair_id="dup", reviewer_id="r2")
    dataset = TrainingDataset(
        schema_version=1, feature_records=(r1, r2), pair_labels=(pair_a, pair_b),
    )
    dataset.validate()  # should not raise -- two distinct reviewers is normal


# ── 5. JSONL round trip / determinism / tamper detection ───────────────────

def test_save_load_jsonl_round_trip(tmp_path):
    r1, r2 = _record(2, 1), _record(1, 1)  # deliberately out of item_ref order
    pair = _pair("1:1", "2:1", choice="left")
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=(pair,))
    path = tmp_path / "dataset.jsonl"
    dataset.save_jsonl(path)

    loaded = TrainingDataset.load_jsonl(path)
    assert [r.item_ref for r in loaded.feature_records] == ["1:1:match_v5", "2:1:match_v5"]
    assert len(loaded.pair_labels) == 1
    assert loaded.pair_labels[0].pair_id == pair.pair_id


def test_save_jsonl_is_deterministic_across_runs(tmp_path):
    r1, r2 = _record(1, 1), _record(1, 2)
    dataset = TrainingDataset(schema_version=1, feature_records=(r2, r1), pair_labels=())
    path_a = tmp_path / "a.jsonl"
    path_b = tmp_path / "b.jsonl"
    dataset.save_jsonl(path_a)
    TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=()).save_jsonl(path_b)
    assert path_a.read_text(encoding="utf-8") == path_b.read_text(encoding="utf-8")


def test_load_jsonl_rejects_unknown_row_kind(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"kind": "not_a_real_kind"}\n', encoding="utf-8")
    with pytest.raises(DatasetValidationError):
        TrainingDataset.load_jsonl(path)


def test_load_jsonl_rejects_header_count_mismatch(tmp_path):
    r1 = _record(1, 1)
    dataset = TrainingDataset(schema_version=1, feature_records=(r1,), pair_labels=())
    path = tmp_path / "dataset.jsonl"
    dataset.save_jsonl(path)

    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["feature_record_count"] = 99  # lie about the count
    lines[0] = json.dumps(header)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(DatasetValidationError):
        TrainingDataset.load_jsonl(path)


def test_load_jsonl_rejects_pair_count_mismatch(tmp_path):
    r1, r2 = _record(1, 1), _record(1, 2)
    pair = _pair("1:1", "1:2", choice="left")
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=(pair,))
    path = tmp_path / "dataset.jsonl"
    dataset.save_jsonl(path)

    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["pair_label_count"] = 5
    lines[0] = json.dumps(header)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(DatasetValidationError):
        TrainingDataset.load_jsonl(path)


# ── 6. select_split ──────────────────────────────────────────────────────────

def test_select_split_returns_only_matching_records():
    r1 = _record(1, 1, split="train")
    r2 = _record(1, 2, split="validation")
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=())
    train_only = select_split(dataset, "train")
    assert [r.item_ref for r in train_only.feature_records] == [r1.item_ref]


def test_select_split_never_falls_back_to_everything():
    r1 = _record(1, 1, split="train")
    dataset = TrainingDataset(schema_version=1, feature_records=(r1,), pair_labels=())
    validation_only = select_split(dataset, "validation")
    assert validation_only.feature_records == ()
    assert validation_only.pair_labels == ()


def test_select_split_resolves_pairs_via_base_ref_within_split():
    r1 = _record(1, 1, split="train")
    r2 = _record(1, 2, split="train")
    r3 = _record(2, 1, split="validation")
    pair_in_split = _pair("1:1", "1:2", choice="left")
    pair_cross_split = _pair("1:1", "2:1", choice="left", pair_id="cross")
    dataset = TrainingDataset(
        schema_version=1, feature_records=(r1, r2, r3),
        pair_labels=(pair_in_split, pair_cross_split),
    )
    train_only = select_split(dataset, "train")
    assert len(train_only.feature_records) == 2
    assert [p.pair_id for p in train_only.pair_labels] == [pair_in_split.pair_id]


# ── 7. StateValueLabel isolation ─────────────────────────────────────────────

def test_build_state_value_labels_from_match():
    match = {
        "game_id": 42, "local_participant_id": 1, "local_win": 1,
        "game_creation_date": "2026-01-01T00:00:00Z",
    }
    participants = [
        {"participant_id": 1, "team_id": 100}, {"participant_id": 2, "team_id": 200},
    ]
    labels = build_state_value_labels_from_match(match, participants)
    by_team = {label.team_id: label.state_value for label in labels}
    assert by_team == {100: 1.0, 200: 0.0}


def test_state_value_label_rejects_non_binary_value():
    with pytest.raises(DatasetValidationError):
        StateValueLabel(
            game_id=1, team_id=100, state_value=0.5, source="match_aggregate_outcome",
            created_at="2026-01-01T00:00:00Z",
        )


def test_state_value_labels_are_isolated_from_feature_records():
    """The auxiliary outcome label lives in its own type/stream and can
    never be constructed as, or merged into, a FeatureRecord."""
    match = {
        "game_id": 42, "local_participant_id": 1, "local_win": 1,
        "game_creation_date": "2026-01-01T00:00:00Z",
    }
    participants = [
        {"participant_id": 1, "team_id": 100}, {"participant_id": 2, "team_id": 200},
    ]
    labels = build_state_value_labels_from_match(match, participants)
    for label in labels:
        label_dict = label.to_dict()
        assert not isinstance(label, FeatureRecord)
        assert "state_value" in label_dict
        # FeatureRecord.features has no legitimate use for a raw outcome
        # value -- confirm none of our own allowlisted specs ever read it,
        # across every tier's canonical contract.
        from score_v2.feature_spec import TIER_FEATURE_CONTRACTS
        for specs in TIER_FEATURE_CONTRACTS.values():
            assert not any("state_value" in spec.path for spec in specs)


def test_state_value_labels_jsonl_round_trip(tmp_path):
    labels = (
        StateValueLabel(game_id=1, team_id=100, state_value=1.0,
                         source="match_aggregate_outcome", created_at="2026-01-01T00:00:00Z"),
        StateValueLabel(game_id=1, team_id=200, state_value=0.0,
                         source="match_aggregate_outcome", created_at="2026-01-01T00:00:00Z"),
    )
    path = tmp_path / "state_values.jsonl"
    save_state_value_labels_jsonl(labels, path)
    loaded = load_state_value_labels_jsonl(path)
    assert loaded == labels


# ── 8. CLI ───────────────────────────────────────────────────────────────────

def test_dataset_cli_validate_ok(tmp_path):
    r1 = _record(1, 1)
    dataset = TrainingDataset(schema_version=1, feature_records=(r1,), pair_labels=())
    path = tmp_path / "dataset.jsonl"
    dataset.save_jsonl(path)
    result = subprocess.run(
        [sys.executable, "-m", "score_v2.training.dataset", "validate", str(path)],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "OK" in result.stdout


def test_dataset_cli_validate_reports_failure(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"kind": "bogus"}\n', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "score_v2.training.dataset", "validate", str(path)],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "INVALID" in result.stdout
