"""Tests for corpus.manifest: deterministic entries, validation, privacy."""

import json

import pytest

from corpus.manifest import (
    CorpusManifest,
    GameMetadata,
    ManifestValidationError,
    build_entry,
    canonical_json,
    hash_identifier,
    sha256_hex,
    validate_entry,
)


def _metadata(**overrides):
    defaults = dict(
        patch="14.1", queue_id=420, map_id=11, duration_seconds=1800,
        game_creation_date="2024-01-01T00:00:00Z",
        region=None, region_unknown_reason="not_captured_by_history_store_schema",
        rank_tier=None, rank_unknown_reason="not_captured_by_history_store_schema",
    )
    defaults.update(overrides)
    return GameMetadata(**defaults)


def _entry(game_id=1, source="aggregate", completeness=1.0, player_keys=None, **kw):
    return build_entry(
        game_id=game_id, source=source, capture_method="lcu_aggregate_fallback",
        player_group_keys=player_keys or ["p_aaa", "p_bbb"],
        completeness=completeness,
        privacy_classification="local_hashed_real",
        consent_status="personal_local_client_data",
        game_metadata=_metadata(), champion="Ahri", role="MID", **kw,
    )


def test_hash_identifier_is_deterministic_and_not_reversible_length():
    salt = b"fixed-salt"
    h1 = hash_identifier("raw-puuid-value", salt)
    h2 = hash_identifier("raw-puuid-value", salt)
    assert h1 == h2
    assert h1.startswith("p_")
    assert len(h1) == len("p_") + 24  # 12 bytes hex


def test_hash_identifier_changes_with_salt():
    a = hash_identifier("raw-puuid-value", b"salt-a")
    b = hash_identifier("raw-puuid-value", b"salt-b")
    assert a != b


def test_build_entry_is_deterministic_given_same_facts():
    e1 = _entry()
    e2 = _entry()
    assert e1.entry_id == e2.entry_id == "1:aggregate"
    assert e1.content_hash == e2.content_hash


def test_content_hash_changes_when_a_fact_changes():
    e1 = _entry(completeness=1.0)
    e2 = _entry(completeness=0.5)
    assert e1.content_hash != e2.content_hash


def test_content_hash_excludes_volatile_timestamp():
    import datetime
    e1 = _entry(now=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc))
    e2 = _entry(now=datetime.datetime(2025, 6, 1, tzinfo=datetime.timezone.utc))
    assert e1.created_at != e2.created_at
    assert e1.content_hash == e2.content_hash


def test_validate_entry_accepts_well_formed_entry():
    assert validate_entry(_entry()) is None


def test_validate_entry_rejects_unknown_source():
    with pytest.raises(ManifestValidationError):
        _entry(source="totally_made_up_source")


def test_validate_entry_rejects_bad_completeness():
    # build_entry() clamps completeness into [0, 1] before validating, so an
    # out-of-range value is only reachable by validating a hand-built entry
    # directly (e.g. one loaded from a tampered manifest file).
    import dataclasses
    entry = dataclasses.replace(_entry(), completeness=1.5)
    with pytest.raises(ManifestValidationError):
        validate_entry(entry)


def test_validate_entry_rejects_credential_shaped_data():
    with pytest.raises(ManifestValidationError):
        _entry(player_keys=["RGAPI-12345678-1234-1234-1234-123456789012"])


def test_validate_entry_rejects_raw_looking_identifier():
    # Mixed-case base64url-ish, 78 chars (real Riot PUUID length) and NOT
    # pure hex, so it isn't mistaken for one of this package's own sha256
    # content hashes.
    raw_puuid = "M9kZqX7bQ1sYpLwN4vTgHje2RcDfA8oIlUnJtEbSaXyPqWmZvKdLrGsNhOiCuTfBwMh123"
    assert len(raw_puuid) >= 70
    with pytest.raises(ManifestValidationError):
        _entry(player_keys=[raw_puuid])


def test_validate_entry_rejects_unknown_privacy_classification():
    with pytest.raises(ManifestValidationError):
        build_entry(
            game_id=1, source="aggregate", capture_method="lcu_aggregate_fallback",
            player_group_keys=["p_aaa"], completeness=1.0,
            privacy_classification="not_a_real_classification",
            consent_status="personal_local_client_data",
            game_metadata=_metadata(),
        )


def test_validate_entry_rejects_unknown_consent_status():
    with pytest.raises(ManifestValidationError):
        build_entry(
            game_id=1, source="aggregate",
            capture_method="lcu_aggregate_fallback",
            player_group_keys=["p_aaa"], completeness=1.0,
            privacy_classification="local_hashed_real",
            consent_status="not_a_real_status",
            game_metadata=_metadata(),
        )


def test_validate_entry_accepts_synthetic_no_consent_status():
    entry = build_entry(
        game_id=1, source="aggregate",
        capture_method="synthetic_adversarial_fixture",
        player_group_keys=["p_synthetic_a", "p_synthetic_b"],
        completeness=1.0,
        privacy_classification="local_hashed_synthetic",
        consent_status="synthetic_no_consent_required",
        game_metadata=_metadata(), champion="SyntheticChampion", role="MID",
    )

    validate_entry(entry)


def test_canonical_json_is_stable_regardless_of_key_order():
    a = canonical_json({"b": 1, "a": 2})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b


def test_sha256_hex_matches_hashlib():
    import hashlib
    assert sha256_hex("hello") == hashlib.sha256(b"hello").hexdigest()


def test_manifest_add_entry_dedups_identical_entry():
    manifest = CorpusManifest()
    manifest.add_entry(_entry())
    manifest.add_entry(_entry())  # identical facts -> no-op, not an error
    assert len(manifest.to_list()) == 1


def test_manifest_add_entry_rejects_conflicting_content_without_overwrite():
    manifest = CorpusManifest()
    manifest.add_entry(_entry(completeness=1.0))
    with pytest.raises(ManifestValidationError):
        manifest.add_entry(_entry(completeness=0.4))


def test_manifest_add_entry_allows_explicit_overwrite():
    manifest = CorpusManifest()
    manifest.add_entry(_entry(completeness=1.0))
    manifest.add_entry(_entry(completeness=0.4), allow_overwrite=True)
    entries = manifest.to_list()
    assert len(entries) == 1
    assert entries[0].completeness == 0.4


def test_manifest_save_load_round_trip(tmp_path):
    manifest = CorpusManifest()
    manifest.add_entry(_entry(game_id=1))
    manifest.add_entry(_entry(game_id=2, player_keys=["p_ccc", "p_ddd"]))
    path = tmp_path / "manifest.json"
    manifest.save(path)

    loaded = CorpusManifest.load(path)
    assert len(loaded.to_list()) == 2
    ids = {e.entry_id for e in loaded.to_list()}
    assert ids == {"1:aggregate", "2:aggregate"}


def test_manifest_save_produces_deterministic_bytes(tmp_path):
    import datetime
    fixed_now = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)

    m1 = CorpusManifest()
    m1.add_entry(_entry(game_id=1, now=fixed_now))
    m1.add_entry(_entry(game_id=2, player_keys=["p_ccc", "p_ddd"], now=fixed_now))
    p1 = tmp_path / "m1.json"
    m1.save(p1)

    m2 = CorpusManifest()
    m2.add_entry(_entry(game_id=2, player_keys=["p_ccc", "p_ddd"], now=fixed_now))
    m2.add_entry(_entry(game_id=1, now=fixed_now))
    p2 = tmp_path / "m2.json"
    m2.save(p2)

    assert p1.read_bytes() == p2.read_bytes()


def test_manifest_stats_reports_source_breakdown_and_mean_completeness():
    manifest = CorpusManifest()
    manifest.add_entry(_entry(game_id=1, completeness=1.0))
    manifest.add_entry(_entry(game_id=2, completeness=0.5, player_keys=["p_ccc"]))
    stats = manifest.stats()
    assert stats["total_entries"] == 2
    assert stats["by_source"] == {"aggregate": 2}
    assert stats["mean_completeness"] == pytest.approx(0.75)


def test_manifest_filter_by_source():
    manifest = CorpusManifest()
    manifest.add_entry(_entry(game_id=1, source="aggregate"))
    filtered = manifest.filter_by(source="aggregate")
    assert len(filtered) == 1
    assert manifest.filter_by(source="match_v5") == []


def test_manifest_load_rejects_malformed_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises((ManifestValidationError, json.JSONDecodeError)):
        CorpusManifest.load(path)


def test_game_metadata_honestly_reports_unknown_region_and_rank():
    metadata = _metadata()
    assert metadata.region is None
    assert metadata.region_unknown_reason
    assert metadata.rank_tier is None
    assert metadata.rank_unknown_reason
