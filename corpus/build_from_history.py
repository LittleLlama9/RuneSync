"""Build sanitized :class:`corpus.manifest.ManifestEntry` objects from a
local ``HistoryStore`` -- read-only, never writes to it.

This is what makes the corpus tooling "usable with local LCU evidence now"
even though bulk/authorized Match-V5 acquisition is separately blocked: it
reads whatever this machine's ``history_store.py`` already has (LCU
timeline, Match-V5 personal-key research payloads if present, Live Client
capture sessions, or the always-available aggregate final stats) and turns
it into a sanitized manifest entry, without ever duplicating the raw
payload or a raw PUUID/summoner name into the manifest.

If a requested source has no evidence stored for a game, this module raises
rather than fabricating a placeholder entry -- see
``docs/CORPUS_AND_REVIEW.md`` "Honesty about missing evidence".
"""

from __future__ import annotations

from typing import Optional

from corpus.manifest import (
    GameMetadata,
    ManifestEntry,
    build_entry,
    hash_identifier,
)


class HistoryEvidenceUnavailableError(Exception):
    """Raised when the requested source has no stored evidence for a game.

    This is the honest-failure path required by the corpus policy: it is
    always preferable to raise this than to silently build an entry that
    claims evidence exists when it does not.
    """


def _local_participant(match: dict, participants: list[dict]) -> dict:
    local_id = match["local_participant_id"]
    for participant in participants:
        if participant["participant_id"] == local_id:
            return participant
    raise HistoryEvidenceUnavailableError(
        f"Game {match['game_id']} has no participant matching "
        f"local_participant_id={local_id}"
    )


def _game_metadata(match: dict) -> GameMetadata:
    # `history_store.py`'s current schema does not capture region/platform
    # or rank/tier at all (only patch, queue, map, duration, and creation
    # time) -- so those two fields are honestly left unknown rather than
    # guessed.
    return GameMetadata(
        patch=match.get("patch"),
        queue_id=match.get("queue_id"),
        map_id=match.get("map_id"),
        duration_seconds=match.get("duration"),
        game_creation_date=match.get("game_creation_date"),
        region=None,
        region_unknown_reason="not_captured_by_history_store_schema",
        rank_tier=None,
        rank_unknown_reason="not_captured_by_history_store_schema",
    )


def build_entry_from_history(
        store, game_id: int, source: str, *, identity_salt: bytes,
        privacy_classification: str = "local_hashed_real") -> ManifestEntry:
    """Build one sanitized manifest entry for ``game_id`` from ``store``.

    ``store`` is any object exposing ``HistoryStore``'s public read API
    (``get_report``, ``get_timeline_payload``, ``list_live_capture_sessions``)
    -- passed by reference rather than imported so this module never needs
    to construct or migrate a real ``HistoryStore`` itself.
    """
    report = store.get_report(game_id)
    if report is None:
        raise HistoryEvidenceUnavailableError(f"No stored match for game {game_id}")
    match, participants = report["match"], report["participants"]

    player_hashes = [
        hash_identifier(participant["puuid"], identity_salt)
        for participant in participants
        if participant.get("puuid")
    ]
    local_participant = _local_participant(match, participants)
    champion = local_participant.get("champion_name")
    role = local_participant.get("role")
    metadata = _game_metadata(match)

    if source == "aggregate":
        return build_entry(
            game_id=game_id, source="aggregate",
            capture_method="lcu_aggregate_fallback",
            player_group_keys=player_hashes,
            completeness=1.0,
            privacy_classification=privacy_classification,
            consent_status="personal_local_client_data",
            game_metadata=metadata, champion=champion, role=role,
        )

    if source in ("lcu_timeline", "match_v5"):
        payload_row = store.get_timeline_payload(game_id, source)
        if payload_row is None:
            raise HistoryEvidenceUnavailableError(
                f"No '{source}' timeline payload is stored for game "
                f"{game_id}; corpus tooling will not fabricate evidence "
                "that was never captured."
            )
        consent = (
            "match_v5_personal_key_research_only" if source == "match_v5"
            else "personal_local_client_data"
        )
        capture_method = (
            "match_v5_personal_key" if source == "match_v5" else "lcu_local_client"
        )
        return build_entry(
            game_id=game_id, source=source, capture_method=capture_method,
            player_group_keys=player_hashes,
            completeness=payload_row.get("completeness", 1.0),
            privacy_classification=privacy_classification,
            consent_status=consent,
            game_metadata=metadata, champion=champion, role=role,
            evidence_content_hash=payload_row.get("content_hash"),
            source_schema_version=str(payload_row.get("schema_version", "1")),
        )

    if source == "live_client":
        sessions = [
            session for session in store.list_live_capture_sessions()
            if session.get("game_id") == game_id
        ]
        if not sessions:
            raise HistoryEvidenceUnavailableError(
                f"No live_client capture session is stored for game "
                f"{game_id}; corpus tooling will not fabricate evidence "
                "that was never captured."
            )
        best = max(sessions, key=lambda s: s.get("completeness", 0.0))
        return build_entry(
            game_id=game_id, source="live_client",
            capture_method="live_client_local_capture",
            player_group_keys=player_hashes,
            completeness=best.get("completeness", 0.0),
            privacy_classification=privacy_classification,
            consent_status="personal_local_client_data",
            game_metadata=metadata, champion=champion, role=role,
            provenance_notes=(
                "Live Client capture is local-player-centric; opponents are "
                "not guaranteed statistical parity with the local player.",
            ),
        )

    raise HistoryEvidenceUnavailableError(f"Unknown evidence source '{source}'")


def available_sources_for_game(store, game_id: int) -> list[str]:
    """Return which evidence sources actually have stored data for ``game_id``.

    Honest by construction: only reports a source as available if the
    corresponding store lookup finds real data, never assumes.
    """
    if store.get_report(game_id) is None:
        return []
    sources = ["aggregate"]
    for source in ("lcu_timeline", "match_v5"):
        if store.get_timeline_payload(game_id, source) is not None:
            sources.append(source)
    if any(
            session.get("game_id") == game_id
            for session in store.list_live_capture_sessions()):
        sources.append("live_client")
    return sources
