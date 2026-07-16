"""Typed provider wrapper around Riot Match-V5 payloads."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol

from riot_api import RiotApiClient, RiotApiError, parse_match_v5_id, regional_route_for_platform


PRIVATE_MATCH_V5_ENV = "RUNESYNC_ENABLE_PRIVATE_RIOT_MATCH_V5"


class TimelineProviderError(Exception):
    """Base class for timeline provider failures."""


class TimelineProviderUpstreamError(TimelineProviderError):
    """Raised when upstream Riot API requests fail."""


class TimelineProviderValidationError(TimelineProviderError):
    """Raised when fetched match data is inconsistent."""


@dataclass(frozen=True)
class TimelineProvenance:
    source: str
    match_id: str
    platform_id: str
    regional_route: str


@dataclass(frozen=True)
class MatchTimelinePayload:
    source: str
    provenance: TimelineProvenance
    match: dict
    timeline: dict


class TimelineProvider(Protocol):
    def fetch_match_timeline(self, match_id: str) -> MatchTimelinePayload:
        """Return a complete match plus timeline payload."""


class RiotMatchV5Provider:
    """Timeline provider backed by Riot Match-V5."""

    def __init__(self, client: RiotApiClient):
        self._client = client

    def fetch_match_timeline(self, match_id: str) -> MatchTimelinePayload:
        normalized_match_id = _normalize_match_id(match_id)
        platform_id, _ = parse_match_v5_id(normalized_match_id)
        try:
            match_payload = self._client.get_match(normalized_match_id)
            timeline_payload = self._client.get_timeline(normalized_match_id)
        except RiotApiError as exc:
            raise TimelineProviderUpstreamError(
                f"Failed to fetch Riot Match-V5 payloads for {normalized_match_id}."
            ) from exc

        _validate_payload_match_id(match_payload, normalized_match_id, "match")
        _validate_payload_match_id(timeline_payload, normalized_match_id, "timeline")
        _validate_participants(match_payload, timeline_payload)

        route = regional_route_for_platform(platform_id)
        provenance = TimelineProvenance(
            source="match_v5",
            match_id=normalized_match_id,
            platform_id=platform_id,
            regional_route=route,
        )
        return MatchTimelinePayload(
            source="match_v5",
            provenance=provenance,
            match=match_payload,
            timeline=timeline_payload,
        )


def is_private_match_v5_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    values = env if env is not None else os.environ
    raw = values.get(PRIVATE_MATCH_V5_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_match_id(match_id: str) -> str:
    platform_id, game_id = parse_match_v5_id(match_id)
    return f"{platform_id}_{game_id}"


def _validate_payload_match_id(payload: dict, expected_match_id: str, label: str) -> None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise TimelineProviderValidationError(f"Riot {label} payload is missing metadata.")
    actual_match_id = metadata.get("matchId")
    if actual_match_id != expected_match_id:
        raise TimelineProviderValidationError(
            f"Riot {label} payload did not match {expected_match_id}."
        )


def _validate_participants(match_payload: dict, timeline_payload: dict) -> None:
    match_metadata = match_payload.get("metadata") or {}
    timeline_metadata = timeline_payload.get("metadata") or {}
    match_participants = match_metadata.get("participants")
    timeline_participants = timeline_metadata.get("participants")

    if isinstance(match_participants, list) and isinstance(timeline_participants, list):
        if match_participants != timeline_participants:
            raise TimelineProviderValidationError("Riot timeline participants did not match.")

    info_participants = (match_payload.get("info") or {}).get("participants")
    if isinstance(match_participants, list) and isinstance(info_participants, list):
        puuids = [participant.get("puuid") for participant in info_participants if isinstance(participant, dict)]
        if len(puuids) != len(match_participants) or set(puuids) != set(match_participants):
            raise TimelineProviderValidationError("Riot match participants were inconsistent.")
