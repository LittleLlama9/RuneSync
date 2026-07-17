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


class TimelineProviderDisabledError(TimelineProviderError):
    """Raised when the private/research Riot Match-V5 feature is not enabled.

    Match-V5 is a private, opt-in research integration (see
    docs/RIOT_API_KEY_POLICY.md). This is enforced here, not just declared by
    ``is_private_match_v5_enabled`` -- callers cannot fetch from Riot Match-V5
    without explicitly opting in via ``RUNESYNC_ENABLE_PRIVATE_RIOT_MATCH_V5``.
    """


class ProviderStatusRecorder(Protocol):
    """Sanitized status sink a Match-V5 provider can report transitions to.

    Implementations must never store token material or raw exception text --
    see ``riot_provider_status.RiotProviderStatusTracker`` for the reference
    implementation.
    """

    def record_success(self) -> None: ...

    def record_error(self, exc: BaseException) -> None: ...

    def record_disabled(self) -> None: ...


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
    """Timeline provider backed by Riot Match-V5.

    This is a private/research integration: ``fetch_match_timeline`` refuses
    to run unless ``is_private_match_v5_enabled`` returns ``True`` for the
    configured environment, so the opt-in gate is enforced here rather than
    left for callers to remember to check.
    """

    def __init__(
        self,
        client: RiotApiClient,
        env: Optional[Mapping[str, str]] = None,
        status_recorder: Optional[ProviderStatusRecorder] = None,
    ):
        self._client = client
        self._env = env
        self._status_recorder = status_recorder

    def fetch_match_timeline(self, match_id: str) -> MatchTimelinePayload:
        if not is_private_match_v5_enabled(self._env):
            if self._status_recorder is not None:
                self._status_recorder.record_disabled()
            raise TimelineProviderDisabledError(
                "Riot Match-V5 is a private research feature and is not "
                "enabled. Set RUNESYNC_ENABLE_PRIVATE_RIOT_MATCH_V5=1 to "
                "opt in."
            )

        normalized_match_id = _normalize_match_id(match_id)
        platform_id, expected_game_id = parse_match_v5_id(normalized_match_id)
        try:
            match_payload = self._client.get_match(normalized_match_id)
            timeline_payload = self._client.get_timeline(normalized_match_id)
        except RiotApiError as exc:
            if self._status_recorder is not None:
                self._status_recorder.record_error(exc)
            raise TimelineProviderUpstreamError(
                f"Failed to fetch Riot Match-V5 payloads for {normalized_match_id}."
            ) from exc

        # Both HTTP requests succeeded: authentication and connectivity are
        # confirmed working regardless of what payload validation below
        # decides. Provider availability and payload validity are separate
        # concerns, so record success here rather than after validation --
        # a validation failure (mismatched match ID, participants, etc.)
        # must not be misreported as an auth/connectivity problem.
        if self._status_recorder is not None:
            self._status_recorder.record_success()

        _validate_payload_match_id(match_payload, normalized_match_id, "match")
        _validate_payload_match_id(timeline_payload, normalized_match_id, "timeline")
        _validate_participants(match_payload, timeline_payload)
        participant_ids = _validate_match_v5_identity(
            match_payload, expected_game_id,
        )

        duration = (match_payload.get("info") or {}).get("gameDuration")
        frames = (timeline_payload.get("info") or {}).get("frames")
        completeness = _validate_timeline_frames(
            frames, participant_ids, duration, label="Riot",
        )

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
            completeness=completeness,
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
    return _validate_timeline_frames(frames, participant_ids, duration, label="Local")


def _validate_timeline_frames(
        frames, participant_ids: set[int], duration, label: str = "Riot") -> float:
    """Validate a timeline's frame list and return a completeness score.

    Shared by both the LCU and Match-V5 providers: both timeline schemas
    describe a list of per-minute frames with ``timestamp``,
    ``participantFrames`` (keyed by participant ID), and ``events``. A
    success-shaped but empty/short payload (no frames, non-increasing
    timestamps, frames missing participant coverage) is rejected here
    rather than silently accepted as a complete timeline.
    """
    if not isinstance(duration, (int, float)) or duration <= 0:
        raise TimelineProviderValidationError(f"{label} match duration was invalid.")
    if not isinstance(frames, list) or not frames:
        raise TimelineProviderValidationError(f"{label} timeline did not contain frames.")

    timestamps = []
    participant_slots = 0
    expected_keys = {str(participant_id) for participant_id in participant_ids}
    for frame in frames:
        if not isinstance(frame, dict):
            raise TimelineProviderValidationError(
                f"{label} timeline contained an invalid frame."
            )
        timestamp = frame.get("timestamp")
        participant_frames = frame.get("participantFrames")
        events = frame.get("events")
        if not isinstance(timestamp, (int, float)) or timestamp < 0:
            raise TimelineProviderValidationError(
                f"{label} timeline contained an invalid timestamp."
            )
        if not isinstance(participant_frames, dict):
            raise TimelineProviderValidationError(
                f"{label} timeline frame was missing participant data."
            )
        if not isinstance(events, list):
            raise TimelineProviderValidationError(
                f"{label} timeline frame was missing its event list."
            )
        actual_keys = {str(key) for key in participant_frames}
        if not actual_keys.issubset(expected_keys):
            raise TimelineProviderValidationError(
                f"{label} timeline contained an unknown participant."
            )
        participant_slots += len(actual_keys)
        timestamps.append(float(timestamp))

    if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
        raise TimelineProviderValidationError(
            f"{label} timeline frame timestamps were not strictly increasing."
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


def platform_id_from_lcu_match(game: dict) -> str:
    """Extract the authoritative Riot platform ID from a local match payload.

    This never guesses at a platform/region: the LCU's own match JSON
    mirrors the Match-v4 schema and carries a top-level ``platformId``
    (e.g. ``"NA1"``), with each participant identity's ``player`` object
    carrying the same value (as ``currentPlatformId``/``platformId``) as a
    fallback if the top-level field is ever absent. If neither is present,
    or the value isn't a platform Riot's regional routing recognizes, this
    fails explicitly instead of assuming a default region.
    """
    if not isinstance(game, dict):
        raise TimelineProviderValidationError(
            "Local match payload is unavailable; cannot determine the "
            "Riot platform ID."
        )
    candidate = game.get("platformId")
    if not isinstance(candidate, str) or not candidate.strip():
        candidate = None
        for identity in game.get("participantIdentities") or []:
            player = (identity or {}).get("player") or {}
            fallback = player.get("currentPlatformId") or player.get("platformId")
            if isinstance(fallback, str) and fallback.strip():
                candidate = fallback
                break
    if not isinstance(candidate, str) or not candidate.strip():
        raise TimelineProviderValidationError(
            "Local match payload did not include a Riot platform ID; "
            "refusing to guess a region for Match-V5 routing."
        )
    normalized = candidate.strip().upper()
    try:
        regional_route_for_platform(normalized)
    except ValueError as exc:
        raise TimelineProviderValidationError(
            f"Local match platform ID {normalized!r} is not a platform "
            "Riot Match-V5 routes."
        ) from exc
    return normalized


def _validate_payload_match_id(payload: dict, expected_match_id: str, label: str) -> None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise TimelineProviderValidationError(f"Riot {label} payload is missing metadata.")
    actual_match_id = metadata.get("matchId")
    if actual_match_id != expected_match_id:
        raise TimelineProviderValidationError(
            f"Riot {label} payload did not match {expected_match_id}."
        )


def _validate_match_v5_identity(match_payload: dict, expected_game_id: int) -> set[int]:
    """Validate the Match-V5 game ID and participant set, returning IDs 1-10.

    A success-shaped but empty/short payload (wrong game, missing or
    partial participants) must not be accepted as a complete match, so this
    checks the numeric ``info.gameId`` against the ID encoded in the
    requested Match-V5 ID and requires exactly ten distinct participant
    slots before any timeline frame validation runs.
    """
    info = match_payload.get("info")
    if not isinstance(info, dict):
        raise TimelineProviderValidationError("Riot match payload is missing info.")
    if info.get("gameId") != expected_game_id:
        raise TimelineProviderValidationError(
            f"Riot match payload game ID did not match {expected_game_id}."
        )
    participants = info.get("participants")
    if not isinstance(participants, list) or len(participants) != 10:
        raise TimelineProviderValidationError(
            "Riot match payload did not contain ten participants."
        )
    participant_ids = set()
    for participant in participants:
        if not isinstance(participant, dict):
            raise TimelineProviderValidationError(
                "Riot match payload contained an invalid participant."
            )
        participant_id = participant.get("participantId")
        puuid = participant.get("puuid")
        if not isinstance(participant_id, int) or participant_id <= 0:
            raise TimelineProviderValidationError(
                "Riot match participant IDs were invalid."
            )
        if not isinstance(puuid, str) or not puuid.strip():
            raise TimelineProviderValidationError(
                "Riot match payload contained an empty participant."
            )
        participant_ids.add(participant_id)
    if len(participant_ids) != 10:
        raise TimelineProviderValidationError(
            "Riot match participant IDs were invalid."
        )
    return participant_ids


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
