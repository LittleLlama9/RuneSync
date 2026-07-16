"""Typed provider wrapper around Riot Match-V5 payloads."""

from __future__ import annotations

import os
import math
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Union

from lcu import LCUConnectionError
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
    completeness: float = 1.0


class TimelineProvider(Protocol):
    def fetch_match_timeline(
            self, match_id: Union[str, int]) -> MatchTimelinePayload:
        """Return a complete match plus timeline payload."""


class LcuTimelineProvider:
    """Post-game timeline provider backed by the local League client."""

    def __init__(self, client):
        self._client = client

    def fetch_match_timeline(
            self, match_id: Union[str, int],
            match_payload: Optional[dict] = None) -> MatchTimelinePayload:
        game_id, platform_id = _parse_lcu_game_id(match_id)
        try:
            resolved_match = match_payload or self._client.get_match_details(game_id)
            timeline_payload = self._client.get_match_timeline(game_id)
        except LCUConnectionError as exc:
            raise TimelineProviderUpstreamError(
                f"Failed to fetch the local timeline for game {game_id}."
            ) from exc

        completeness = _validate_lcu_payloads(
            resolved_match, timeline_payload, game_id,
        )
        canonical_match_id = (
            f"{platform_id}_{game_id}" if platform_id else str(game_id)
        )
        provenance = TimelineProvenance(
            source="lcu_timeline",
            match_id=canonical_match_id,
            platform_id=platform_id,
            regional_route="LOCAL",
        )
        return MatchTimelinePayload(
            source="lcu_timeline",
            provenance=provenance,
            match=resolved_match,
            timeline=timeline_payload,
            completeness=completeness,
        )


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


def _parse_lcu_game_id(match_id: Union[str, int]) -> tuple[int, str]:
    if isinstance(match_id, bool):
        raise ValueError("Game ID must be a positive integer.")
    if isinstance(match_id, int):
        if match_id <= 0:
            raise ValueError("Game ID must be a positive integer.")
        return match_id, ""
    if not isinstance(match_id, str):
        raise ValueError("Game ID must be numeric or a Match-V5 ID.")
    normalized = match_id.strip().upper()
    if normalized.isdigit():
        game_id = int(normalized)
        if game_id > 0:
            return game_id, ""
    platform_id, game_id = parse_match_v5_id(normalized)
    return game_id, platform_id


def _validate_lcu_payloads(
        match_payload: dict, timeline_payload: dict, game_id: int) -> float:
    if not isinstance(match_payload, dict) \
            or match_payload.get("gameId") != game_id:
        raise TimelineProviderValidationError(
            f"Local match payload did not match game {game_id}."
        )
    participants = match_payload.get("participants")
    if not isinstance(participants, list) or len(participants) != 10:
        raise TimelineProviderValidationError(
            "Local match payload did not contain ten participants."
        )
    participant_ids = {
        participant.get("participantId")
        for participant in participants if isinstance(participant, dict)
    }
    if len(participant_ids) != 10 or any(
            not isinstance(participant_id, int) or participant_id <= 0
            for participant_id in participant_ids):
        raise TimelineProviderValidationError(
            "Local match participant IDs were invalid."
        )

    duration = match_payload.get("gameDuration")
    frames = (
        timeline_payload.get("frames")
        if isinstance(timeline_payload, dict) else None
    )
    if not isinstance(duration, (int, float)) or duration <= 0:
        raise TimelineProviderValidationError(
            "Local match duration was invalid."
        )
    if not isinstance(frames, list) or not frames:
        raise TimelineProviderValidationError(
            "Local timeline did not contain frames."
        )

    timestamps = []
    participant_slots = 0
    expected_keys = {str(participant_id) for participant_id in participant_ids}
    for frame in frames:
        if not isinstance(frame, dict):
            raise TimelineProviderValidationError(
                "Local timeline contained an invalid frame."
            )
        timestamp = frame.get("timestamp")
        participant_frames = frame.get("participantFrames")
        events = frame.get("events")
        if not isinstance(timestamp, (int, float)) or timestamp < 0:
            raise TimelineProviderValidationError(
                "Local timeline contained an invalid timestamp."
            )
        if not isinstance(participant_frames, dict):
            raise TimelineProviderValidationError(
                "Local timeline frame was missing participant data."
            )
        if not isinstance(events, list):
            raise TimelineProviderValidationError(
                "Local timeline frame was missing its event list."
            )
        actual_keys = {str(key) for key in participant_frames}
        if not actual_keys.issubset(expected_keys):
            raise TimelineProviderValidationError(
                "Local timeline contained an unknown participant."
            )
        participant_slots += len(actual_keys)
        timestamps.append(float(timestamp))

    if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
        raise TimelineProviderValidationError(
            "Local timeline frame timestamps were not strictly increasing."
        )

    expected_frames = math.ceil(float(duration) / 60.0) + 1
    duration_coverage = min(timestamps[-1] / (float(duration) * 1000.0), 1.0)
    frame_coverage = min(len(frames) / expected_frames, 1.0)
    participant_coverage = participant_slots / (
        len(frames) * len(participant_ids)
    )
    return round(
        duration_coverage * frame_coverage * participant_coverage, 4
    )


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
