"""Tests for the ``game_ids`` filter added to ``export_panel_inputs``.

The filter lets the panel grade a stratified sample of a large corpus rather
than every retained game, and must fail loudly if a requested game has no
matching evidence-source feature set so a sampling plan can never silently
shrink.
"""

import json

import pytest

from corpus.synthetic_panel import PanelValidationError, export_panel_inputs


def _feature_set(game_id, *, evidence_source="match_v5"):
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
            "raw": {"kills": participant_id},
            "fight_influence": {"kill_events": participant_id},
        }
    return {
        "game_id": game_id,
        "evidence_source": evidence_source,
        "input_hash": f"{game_id:064d}",
        "features": {
            "feature_version": "2.0.0-evidence",
            "evidence_source": evidence_source,
            "chosen_source_completeness": 1.0,
            "duration_seconds": 1800,
            "participants": participants,
        },
    }


class _FakeStore:
    def __init__(self, feature_sets):
        self._feature_sets = feature_sets

    def list_feature_sets(self):
        return list(self._feature_sets)


def _read_packet_game_ids(output_dir):
    """Recover the source game count from a round file."""
    rows = [
        json.loads(line)
        for line in (output_dir / "round-a.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]
    return rows


def test_game_ids_filter_restricts_export(tmp_path):
    store = _FakeStore([_feature_set(g) for g in (10, 20, 30, 40)])
    result = export_panel_inputs(
        store, tmp_path, evidence_source="match_v5", game_ids=[20, 40],
    )
    assert result["games"] == 2
    rows = _read_packet_game_ids(tmp_path)
    assert len(rows) == 2


def test_game_ids_none_exports_everything(tmp_path):
    store = _FakeStore([_feature_set(g) for g in (10, 20, 30)])
    result = export_panel_inputs(store, tmp_path, evidence_source="match_v5")
    assert result["games"] == 3


def test_missing_requested_game_raises(tmp_path):
    store = _FakeStore([_feature_set(g) for g in (10, 20)])
    with pytest.raises(PanelValidationError) as exc:
        export_panel_inputs(
            store, tmp_path, evidence_source="match_v5", game_ids=[10, 99],
        )
    assert "99" in str(exc.value)


def test_evidence_source_mismatch_is_not_selected(tmp_path):
    store = _FakeStore([
        _feature_set(10, evidence_source="match_v5"),
        _feature_set(20, evidence_source="lcu_timeline"),
    ])
    # game 20 exists but under a different tier -> requesting it must raise
    with pytest.raises(PanelValidationError):
        export_panel_inputs(
            store, tmp_path, evidence_source="match_v5", game_ids=[10, 20],
        )
