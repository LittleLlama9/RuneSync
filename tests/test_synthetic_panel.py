import json

import pytest

from corpus.synthetic_panel import (
    PANEL_ROUNDS,
    PanelValidationError,
    RUBRIC_HASH,
    aggregate_judgments,
    build_match_packet,
    load_judgments,
    load_packets,
    validate_panel_bundle,
    validate_judgment,
)


def _stored_feature_set(game_id=123):
    participants = {}
    for participant_id in range(1, 11):
        participants[str(participant_id)] = {
            "team_id": 100 if participant_id <= 5 else 200,
            "baseline": {
                "champion": f"Champion{participant_id}",
                "role": ("top", "jungle", "mid", "bot", "support")[
                    (participant_id - 1) % 5
                ],
            },
            "raw": {
                "kills": participant_id,
                "deaths": 11 - participant_id,
                "assists": participant_id + 2,
            },
            "fight_influence": {
                "kill_events": participant_id,
                "untraded_deaths": participant_id % 3,
            },
            "resource_conversion": {
                "lane_opponent": (
                    participant_id + 5
                    if participant_id <= 5 else participant_id - 5
                ),
                "conversion_rate": participant_id / 10,
            },
        }
    return {
        "game_id": game_id,
        "evidence_source": "lcu_timeline",
        "input_hash": "a" * 64,
        "features": {
            "feature_version": "2.0.0-evidence",
            "evidence_source": "lcu_timeline",
            "chosen_source_completeness": 1.0,
            "duration_seconds": 1800,
            "participants": participants,
        },
    }


def _judgment(packet, *, reverse=False, confidence=0.9):
    subjects = [row["subject_id"] for row in packet["participants"]]
    subjects = sorted(subjects, reverse=reverse)
    by_subject = {
        row["subject_id"]: row for row in packet["participants"]
    }
    return {
        "packet_id": packet["packet_id"],
        "ranking_tiers": [[subject_id] for subject_id in subjects],
        "assessments": [
            {
                "subject_id": subject_id,
                "confidence": confidence,
                "rationale_tags": ["combat_impact"],
                "evidence_paths": ["fight_influence.kill_events"],
                "brief_reason": (
                    f"Contextual combat evidence is "
                    f"{by_subject[subject_id]['features']['fight_influence']['kill_events']}."
                ),
            }
            for subject_id in subjects
        ],
        "overall_confidence": confidence,
        "abstain_reason": "",
    }


def _judgment_for_base_order(packet, private_map, ordered_base_refs):
    subject_by_ref = {
        base_ref: subject_id
        for subject_id, base_ref in private_map["subjects"].items()
    }
    subjects = [subject_by_ref[base_ref] for base_ref in ordered_base_refs]
    row = _judgment(packet)
    row["ranking_tiers"] = [[subject_id] for subject_id in subjects]
    return validate_judgment(row, packet)


def test_match_packets_are_blinded_and_round_shuffled():
    stored = _stored_feature_set()
    packet_a, private_a = build_match_packet(stored, 123, "a")
    packet_b, private_b = build_match_packet(stored, 123, "b")

    assert packet_a["rubric_hash"] == RUBRIC_HASH
    assert packet_a["packet_id"] != packet_b["packet_id"]
    assert set(private_a["subjects"].values()) == {
        f"123:{participant_id}" for participant_id in range(1, 11)
    }
    assert set(private_b["subjects"].values()) == set(
        private_a["subjects"].values()
    )
    serialized = json.dumps((packet_a, packet_b)).lower()
    for forbidden in (
            "game_id", "participant_id", "summoner_name", "puuid",
            '"win"', "total_score", "match_rank", '"lane_opponent"'):
        assert forbidden not in serialized
    assert "lane_opponent_subject_id" in serialized


def test_match_packet_rejects_outcome_leakage():
    stored = _stored_feature_set()
    stored["features"]["participants"]["1"]["win"] = True
    with pytest.raises(Exception, match="outcome"):
        build_match_packet(stored, 123, "a")


def test_match_packet_rejects_existing_score_leakage():
    stored = _stored_feature_set()
    stored["features"]["participants"]["1"]["total_score"] = 99.0
    with pytest.raises(PanelValidationError, match="score/rank leakage"):
        build_match_packet(stored, 123, "a")


def test_judgment_requires_all_subjects_and_real_evidence_paths():
    packet, _ = build_match_packet(_stored_feature_set(), 123, "a")
    row = _judgment(packet)
    assert validate_judgment(row, packet).overall_confidence == 0.9

    row["ranking_tiers"] = row["ranking_tiers"][:-1]
    with pytest.raises(PanelValidationError, match="every packet subject"):
        validate_judgment(row, packet)

    row = _judgment(packet)
    row["assessments"][0]["evidence_paths"] = ["not.a.real.path"]
    with pytest.raises(PanelValidationError, match="does not exist"):
        validate_judgment(row, packet)

    row = _judgment(packet)
    row["assessments"].append(dict(row["assessments"][0]))
    with pytest.raises(PanelValidationError, match="duplicate subjects"):
        validate_judgment(row, packet)


def test_load_judgments_rejects_unknown_packet(tmp_path):
    path = tmp_path / "judge.jsonl"
    path.write_text(json.dumps({
        "packet_id": "unknown",
        "ranking_tiers": [],
        "assessments": [],
        "overall_confidence": 0.5,
        "abstain_reason": "No packet.",
    }) + "\n", encoding="utf-8")
    with pytest.raises(PanelValidationError, match="unknown packet_id"):
        load_judgments(path, {})


def test_load_judgments_rejects_incomplete_file(tmp_path):
    packet_a, _ = build_match_packet(_stored_feature_set(123), 123, "a")
    packet_b, _ = build_match_packet(_stored_feature_set(456), 456, "a")
    path = tmp_path / "judge.jsonl"
    path.write_text(
        json.dumps(_judgment(packet_a)) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PanelValidationError, match="missing 1 packet"):
        load_judgments(
            path,
            {packet_a["packet_id"]: packet_a, packet_b["packet_id"]: packet_b},
        )


def test_load_packets_rejects_tampered_packet_body(tmp_path):
    packet, _ = build_match_packet(_stored_feature_set(), 123, "a")
    packet["duration_seconds"] = 9999
    path = tmp_path / "round-a.jsonl"
    path.write_text(json.dumps(packet) + "\n", encoding="utf-8")

    with pytest.raises(PanelValidationError, match="packet body hash mismatch"):
        load_packets(path)


def test_panel_bundle_rejects_private_subject_mismatch():
    packets_by_round = {}
    private_maps = {}
    for round_id in PANEL_ROUNDS:
        packet, private_map = build_match_packet(
            _stored_feature_set(), 123, round_id,
        )
        packets_by_round[round_id] = {packet["packet_id"]: packet}
        private_maps[packet["packet_id"]] = private_map
    first_map = next(iter(private_maps.values()))
    first_subject = next(iter(first_map["subjects"]))
    del first_map["subjects"][first_subject]

    with pytest.raises(PanelValidationError, match="subject mismatch"):
        validate_panel_bundle(
            packets_by_round,
            {
                "schema_version": 1,
                "rubric_hash": RUBRIC_HASH,
                "packets": private_maps,
            },
        )


def test_panel_bundle_rejects_private_subject_permutation():
    packets_by_round = {}
    private_maps = {}
    for round_id in PANEL_ROUNDS:
        packet, private_map = build_match_packet(
            _stored_feature_set(), 123, round_id,
        )
        packets_by_round[round_id] = {packet["packet_id"]: packet}
        private_maps[packet["packet_id"]] = private_map
    packet_b = next(iter(packets_by_round["b"].values()))
    private_b = private_maps[packet_b["packet_id"]]
    subject_a, subject_b = list(private_b["subjects"])[:2]
    private_b["subjects"][subject_a], private_b["subjects"][subject_b] = (
        private_b["subjects"][subject_b], private_b["subjects"][subject_a],
    )

    with pytest.raises(PanelValidationError, match="binding mismatch"):
        validate_panel_bundle(
            packets_by_round,
            {
                "schema_version": 1,
                "rubric_hash": RUBRIC_HASH,
                "packets": private_maps,
            },
        )


def test_aggregate_requires_order_stability_and_unanimity():
    stored = _stored_feature_set()
    packets_by_round = {}
    private_maps = {}
    ordered_refs = [f"123:{participant_id}" for participant_id in range(1, 11)]
    for round_id in PANEL_ROUNDS:
        packet, private_map = build_match_packet(stored, 123, round_id)
        packets_by_round[round_id] = {packet["packet_id"]: packet}
        private_maps[packet["packet_id"]] = private_map

    judgments = {}
    for reviewer_id in ("alpha", "beta", "gamma"):
        for round_id in PANEL_ROUNDS:
            packet = next(iter(packets_by_round[round_id].values()))
            private_map = private_maps[packet["packet_id"]]
            judgments[(reviewer_id, round_id)] = [
                _judgment_for_base_order(packet, private_map, ordered_refs)
            ]

    labels, token_maps, report = aggregate_judgments(
        packets_by_round=packets_by_round,
        private_maps=private_maps,
        judgments=judgments,
        generated_at="2026-07-17T09:00:00+00:00",
        max_tier_gap=9,
    )
    assert report["accepted_consensus_pairs"] == 45
    assert len(labels) == 45
    assert len(token_maps) == 45
    assert {
        label.reviewer_id for label in labels
    } == {"synthetic:panel-1.0.0"}

    packet_b = next(iter(packets_by_round["b"].values()))
    private_b = private_maps[packet_b["packet_id"]]
    judgments[("gamma", "b")] = [
        _judgment_for_base_order(
            packet_b, private_b, list(reversed(ordered_refs)),
        )
    ]
    labels, _, report = aggregate_judgments(
        packets_by_round=packets_by_round,
        private_maps=private_maps,
        judgments=judgments,
        generated_at="2026-07-17T09:00:00+00:00",
        max_tier_gap=9,
    )
    assert report["accepted_consensus_pairs"] == 0
    assert labels == []


def test_aggregate_requires_both_rounds_for_every_reviewer():
    stored = _stored_feature_set()
    packets_by_round = {}
    private_maps = {}
    for round_id in PANEL_ROUNDS:
        packet, private_map = build_match_packet(stored, 123, round_id)
        packets_by_round[round_id] = {packet["packet_id"]: packet}
        private_maps[packet["packet_id"]] = private_map
    packet_a = next(iter(packets_by_round["a"].values()))
    private_a = private_maps[packet_a["packet_id"]]
    ordered_refs = [f"123:{participant_id}" for participant_id in range(1, 11)]

    with pytest.raises(PanelValidationError, match="missing rounds"):
        aggregate_judgments(
            packets_by_round=packets_by_round,
            private_maps=private_maps,
            judgments={
                ("alpha", "a"): [
                    _judgment_for_base_order(
                        packet_a, private_a, ordered_refs,
                    )
                ],
            },
            generated_at="2026-07-17T09:00:00+00:00",
            min_reviewers=1,
            max_tier_gap=9,
        )


def test_aggregate_accepts_minimum_stable_reviewers_when_an_extra_is_unstable():
    stored = _stored_feature_set()
    packets_by_round = {}
    private_maps = {}
    ordered_refs = [f"123:{participant_id}" for participant_id in range(1, 11)]
    for round_id in PANEL_ROUNDS:
        packet, private_map = build_match_packet(stored, 123, round_id)
        packets_by_round[round_id] = {packet["packet_id"]: packet}
        private_maps[packet["packet_id"]] = private_map

    judgments = {}
    for reviewer_id in ("alpha", "beta", "gamma", "unstable"):
        for round_id in PANEL_ROUNDS:
            packet = next(iter(packets_by_round[round_id].values()))
            private_map = private_maps[packet["packet_id"]]
            order = ordered_refs
            if reviewer_id == "unstable" and round_id == "b":
                order = list(reversed(order))
            judgments[(reviewer_id, round_id)] = [
                _judgment_for_base_order(packet, private_map, order)
            ]

    labels, _, report = aggregate_judgments(
        packets_by_round=packets_by_round,
        private_maps=private_maps,
        judgments=judgments,
        generated_at="2026-07-17T09:00:00+00:00",
        min_reviewers=3,
        max_tier_gap=9,
    )
    assert report["accepted_consensus_pairs"] == 45
    assert len(labels) == 45
