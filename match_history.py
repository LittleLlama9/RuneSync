"""Post-game match normalization, backfill, and ingestion orchestration."""

import datetime
import sqlite3
import threading
import time
from dataclasses import asdict
from itertools import permutations
from typing import Callable, Optional

from champion_roles import get_role_weights
from history_store import HistoryStore
from lcu import LCUConnectionError
from performance_score import (
    SCORING_MODEL_VERSION,
    ScoreRouter,
    ScoreRoutingError,
    score_match,
)
from riot_api import RiotApiClient, RiotApiError, build_match_v5_id
from riot_provider_status import ProviderStatus, get_default_status_tracker
from secret_store import SecretStoreError, SecretStoreStatus
from score_features import (
    AGGREGATE,
    FEATURE_VERSION,
    SOURCE_PRIORITY,
    detect_capabilities,
    extract_game_features,
)
from score_v2.coaching import MIN_TIMELINE_COMPLETENESS
from timeline_provider import (
    LcuTimelineProvider,
    RiotMatchV5Provider,
    TimelineProviderDisabledError,
    TimelineProviderError,
    TimelineProviderValidationError,
    is_private_match_v5_enabled,
    platform_id_from_lcu_match,
)


SUPPORTED_QUEUE_IDS = {400, 420, 430, 440, 480, 490}
SUMMONERS_RIFT_MAP_ID = 11
TEAM_ROLES = ("top", "jungle", "mid", "bot", "support")
MATCH_V5_SOURCE = "match_v5"


class UnsupportedMatch(ValueError):
    pass


def _number(value, default=0) -> int:
    return int(value) if isinstance(value, (int, float)) else default


def _identity_map(game: dict) -> dict[int, dict]:
    out = {}
    for identity in game.get("participantIdentities") or []:
        participant_id = identity.get("participantId")
        player = identity.get("player") or {}
        if isinstance(participant_id, int):
            out[participant_id] = player
    return out


def _display_name(player: dict, participant_id: int) -> str:
    game_name = player.get("gameName") or ""
    tag_line = player.get("tagLine") or ""
    if game_name and tag_line:
        return f"{game_name}#{tag_line}"
    return game_name or player.get("summonerName") or f"Player {participant_id}"


def _timeline_role(participant: dict) -> str:
    timeline = participant.get("timeline") or {}
    lane = (timeline.get("lane") or "").upper()
    role = (timeline.get("role") or "").upper()
    if lane == "JUNGLE":
        return "jungle"
    if lane == "TOP" and role != "DUO":
        return "top"
    if lane == "MIDDLE" and role != "DUO":
        return "mid"
    if lane == "BOTTOM":
        if "SUPPORT" in role:
            return "support"
        if "CARRY" in role:
            return "bot"
        return ""
    return ""


def _resolve_roles(players: list[dict], captured_positions: Optional[dict[str, str]]) -> None:
    captured_positions = captured_positions or {}
    for team_id in {player["team_id"] for player in players}:
        team = sorted(
            (player for player in players if player["team_id"] == team_id),
            key=lambda player: player["participant_id"],
        )
        if len(team) != 5:
            for player in team:
                player["role"] = player["role"] or "unknown"
            continue

        best_score = float("-inf")
        best_roles = TEAM_ROLES
        for candidate_roles in permutations(TEAM_ROLES):
            score = 0.0
            for player, candidate in zip(team, candidate_roles):
                played = player["role"]
                captured = captured_positions.get(player["puuid"], "")
                if played == candidate:
                    score += 1000.0
                if captured == candidate:
                    score += 800.0
                score += get_role_weights(player["champion_name"]).get(candidate, 0.0)
            if score > best_score:
                best_score = score
                best_roles = candidate_roles

        for player, role in zip(team, best_roles):
            player["role"] = role


def normalize_match(
        game: dict, local_puuid: str, champion_names: dict[int, str],
        captured_positions: Optional[dict[str, str]] = None) -> dict:
    game_id = game.get("gameId")
    queue_id = game.get("queueId")
    map_id = game.get("mapId")
    duration = _number(game.get("gameDuration"))
    participants_raw = game.get("participants") or []
    if not isinstance(game_id, int) or game_id <= 0:
        raise ValueError("Match payload has no valid game ID")
    if queue_id not in SUPPORTED_QUEUE_IDS or map_id != SUMMONERS_RIFT_MAP_ID:
        raise UnsupportedMatch(f"Queue {queue_id} on map {map_id} is not scored")
    if len(participants_raw) != 10:
        raise UnsupportedMatch("DAEMON Score requires a complete 10-player match")
    if duration < 300 or any(
            (participant.get("stats") or {}).get("gameEndedInEarlySurrender")
            for participant in participants_raw):
        raise UnsupportedMatch("Remakes are not scored")

    identities = _identity_map(game)
    players = []
    local_participant_id = None
    for participant in participants_raw:
        participant_id = _number(participant.get("participantId"))
        stats = participant.get("stats") or {}
        identity = identities.get(participant_id, {})
        puuid = identity.get("puuid") or ""
        if puuid == local_puuid:
            local_participant_id = participant_id
        champion_id = _number(participant.get("championId"))
        players.append({
            "participant_id": participant_id,
            "puuid": puuid,
            "summoner_name": _display_name(identity, participant_id),
            "champion_id": champion_id,
            "champion_name": champion_names.get(champion_id, f"Champion {champion_id}"),
            "team_id": _number(participant.get("teamId")),
            "role": _timeline_role(participant),
            "win": bool(stats.get("win")),
            "kills": _number(stats.get("kills")),
            "deaths": _number(stats.get("deaths")),
            "assists": _number(stats.get("assists")),
            "gold_earned": _number(stats.get("goldEarned")),
            "cs": _number(stats.get("totalMinionsKilled"))
                  + _number(stats.get("neutralMinionsKilled")),
            "champion_level": _number(stats.get("champLevel")),
            "damage_to_champions": _number(stats.get("totalDamageDealtToChampions")),
            "damage_to_objectives": _number(stats.get("damageDealtToObjectives")),
            "damage_to_turrets": _number(stats.get("damageDealtToTurrets")),
            "damage_taken": _number(stats.get("totalDamageTaken")),
            "damage_mitigated": _number(stats.get("damageSelfMitigated")),
            "healing": _number(stats.get("totalHeal")),
            "vision_score": _number(stats.get("visionScore")),
            "wards_placed": _number(stats.get("wardsPlaced")),
            "wards_killed": _number(stats.get("wardsKilled")),
            "items": [
                item_id for item_id in (
                    _number(stats.get(f"item{slot}")) for slot in range(7)
                ) if item_id > 0
            ],
        })

    if local_participant_id is None:
        raise ValueError("Local player was not found in the match payload")
    _resolve_roles(players, captured_positions)
    local_player = next(
        player for player in players
        if player["participant_id"] == local_participant_id
    )
    scores = score_match(players, duration)

    creation = _number(game.get("gameCreation"))
    creation_date = game.get("gameCreationDate") or datetime.datetime.fromtimestamp(
        creation / 1000 if creation > 10_000_000_000 else creation,
        tz=datetime.timezone.utc,
    ).isoformat()
    return {
        "match": {
            "game_id": game_id,
            "queue_id": queue_id,
            "map_id": map_id,
            "game_mode": game.get("gameMode") or "",
            "game_creation": creation,
            "game_creation_date": creation_date,
            "duration": duration,
            "patch": game.get("gameVersion") or "",
            "local_participant_id": local_participant_id,
            "local_win": local_player["win"],
            "local_champion_id": local_player["champion_id"],
            "local_champion_name": local_player["champion_name"],
            "local_role": local_player["role"],
            "score_model_version": SCORING_MODEL_VERSION,
        },
        "participants": players,
        "scores": scores,
    }


class MatchHistoryService:
    def __init__(
            self, lcu, store: Optional[HistoryStore] = None,
            on_log: Optional[Callable[[str, str], None]] = None,
            on_updated: Optional[Callable[[], None]] = None,
            on_postgame: Optional[Callable[[int], None]] = None,
            riot_status_tracker=None,
            riot_client_factory: Optional[Callable[[Callable[[], str]], object]] = None,
            match_v5_scheduler: Optional[
                Callable[[Callable[[], None]], None]
            ] = None,
            score_v2_artifacts: Optional[dict] = None,
            allow_development_score_v2: bool = False):
        self.lcu = lcu
        self.store = store or HistoryStore()
        self.on_log = on_log or (lambda message, tag="info": None)
        self.on_updated = on_updated
        self.on_postgame = on_postgame
        self._sync_lock = threading.Lock()
        self._ingest_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_game_id: Optional[int] = None
        self._active_positions: dict[str, str] = {}
        self._champion_names: dict[int, str] = {}
        self._lcu_timeline_provider = LcuTimelineProvider(lcu)
        # Builds the Riot Match-V5 HTTP client from a key supplier. Tests
        # inject a fake in place of RiotApiClient so Match-V5 integration
        # behavior (routing, caching, backoff, validation) can be exercised
        # without any real network access.
        self._riot_client_factory = riot_client_factory or (
            lambda key_supplier: RiotApiClient(key_supplier=key_supplier)
        )
        # Match-V5 is a private, opt-in upgrade path (see
        # docs/RIOT_API_KEY_POLICY.md): defaults to the process-wide
        # sanitized status tracker so a request's outcome is reflected in
        # later status checks, but tests can inject their own tracker
        # (backed by a throwaway secret store) without touching the real
        # DPAPI-protected key on disk.
        self._riot_status_tracker = riot_status_tracker or get_default_status_tracker()
        self._match_v5_fetch_lock = threading.Lock()
        self._match_v5_scheduler = match_v5_scheduler or (
            lambda task: threading.Thread(target=task, daemon=True).start()
        )
        self._score_router = ScoreRouter(
            score_v2_artifacts,
            allow_development_artifacts=allow_development_score_v2,
        )

    def _ensure_champions(self) -> dict[int, str]:
        if not self._champion_names:
            self._champion_names = self.lcu.get_champion_name_map()
        return self._champion_names

    @property
    def active_game_id(self) -> Optional[int]:
        """The authoritative LCU game ID for the currently active game, if
        known. Used by the Live Client Data collector (live_client.py) to
        reconcile its capture sessions -- the Live Client Data API itself
        never reports a game/match ID."""
        with self._state_lock:
            return self._active_game_id

    def capture_active_game(self) -> None:
        session = self.lcu.get_gameflow_session() or {}
        game_data = session.get("gameData") or {}
        game_id = game_data.get("gameId")
        active_game_id = game_id if isinstance(game_id, int) and game_id > 0 else None
        position_map = {
            "TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid",
            "BOTTOM": "bot", "UTILITY": "support",
        }
        positions = {}
        for team_name in ("teamOne", "teamTwo"):
            for player in game_data.get(team_name) or []:
                puuid = player.get("puuid") or ""
                position = position_map.get(
                    (player.get("selectedPosition") or "").upper(), ""
                )
                if puuid and position:
                    positions[puuid] = position
        with self._state_lock:
            self._active_game_id = active_game_id
            self._active_positions = positions

    def ingest_game(
            self, game_id: int, captured_positions: Optional[dict[str, str]] = None,
            notify: bool = True,
            log_timeline_failure: bool = True,
            attempt_match_v5: bool = True) -> Optional[dict]:
        match_v5_game = None
        with self._ingest_lock:
            if self.store.has_game(game_id):
                self._save_lcu_timeline(
                    game_id, log_failure=log_timeline_failure,
                )
                report = self.store.get_report(game_id)
            else:
                local_puuid = self.lcu.get_current_summoner_puuid()
                if not local_puuid:
                    raise RuntimeError("Current summoner PUUID is unavailable")
                game = self.lcu.get_match_details(game_id)
                try:
                    report = normalize_match(
                        game, local_puuid, self._ensure_champions(),
                        captured_positions,
                    )
                except UnsupportedMatch as e:
                    self.on_log(f"History skipped game {game_id}: {e}", "info")
                    return None
                self.store.save_report(report)
                self._save_lcu_timeline(
                    game_id, game, log_failure=log_timeline_failure,
                )
                match_v5_game = game
            self.refresh_score_v2(
                game_id, log_failure=log_timeline_failure,
            )
            if notify and self.on_updated:
                self.on_updated()
        # Match-V5 is a private, additive upgrade. Schedule it only after the
        # LCU report/timeline are durable and the global ingestion lock is
        # released, so remote latency can never block another game's local
        # postgame report.
        if attempt_match_v5:
            self._schedule_match_v5_upgrade(
                game_id, match_v5_game, log_timeline_failure,
            )
        return report

    def _schedule_match_v5_upgrade(
            self, game_id: int, game_payload: Optional[dict],
            log_failure: bool) -> None:
        if not is_private_match_v5_enabled():
            return
        self._match_v5_scheduler(
            lambda: self._save_match_v5_timeline(
                game_id, game_payload, log_failure=log_failure,
            )
        )

    def refresh_score_v2(
            self, game_id: int, *, log_failure: bool = True) -> Optional[int]:
        """Append and conditionally activate the best available Score v2 run.

        With no registered production artifact this is a no-op, preserving v1.
        When artifacts are present, the strongest evidence tier that has an
        exact matching artifact is extracted and scored. Weaker reruns remain
        immutable but cannot replace a stronger active tier.
        """
        if not self._score_router.enabled:
            return None
        try:
            capabilities = detect_capabilities(self.store, game_id)
            available_sources = [
                source for source in SOURCE_PRIORITY
                if (
                    capabilities.aggregate if source == AGGREGATE
                    else getattr(capabilities, source)
                )
            ]
            source = self._score_router.select_source(
                available_sources, capabilities.quality_dict(),
            )
            if source is None:
                return None
            features = extract_game_features(
                self.store, game_id, FEATURE_VERSION,
                evidence_source=source,
            )
            stored = self.store.get_feature_set(
                game_id, feature_version=FEATURE_VERSION,
                evidence_source=source,
            )
            if stored is None:
                raise ScoreRoutingError(
                    f"Score v2 feature set was not persisted for game {game_id}"
                )
            match = self.store.get_match(game_id)
            if match is None:
                raise ScoreRoutingError(f"Match {game_id} disappeared before scoring")
            recent_local_features = self.store.list_recent_local_feature_blocks(
                game_id, FEATURE_VERSION, source,
                min_completeness=MIN_TIMELINE_COMPLETENESS,
            )
            routed = self._score_router.score_feature_set(
                features, stored["evidence"],
                local_participant_id=match["local_participant_id"],
                recent_local_features=recent_local_features,
            )
            run_id = self.store.save_score_run(
                game_id, list(routed.scores),
                model_version=routed.model_version,
                feature_version=routed.feature_version,
                evidence_source=routed.evidence_source,
                calibration_version=routed.calibration_version,
                model_artifact_hash=routed.model_artifact_hash,
                artifact_model_version=routed.artifact_model_version,
                model_family=routed.model_family,
                input_hash=stored["input_hash"],
                confidence=dict(routed.confidence),
                activate=False,
            )
            self.store.activate_score_run_if_preferred(run_id)
            return run_id
        except (
                OSError, sqlite3.Error, ScoreRoutingError,
                TypeError, ValueError) as exc:
            if log_failure:
                self.on_log(
                    f"History could not append Score v2 for game {game_id}: {exc}",
                    "warn",
                )
            return None

    def _save_lcu_timeline(
            self, game_id: int, match_payload: Optional[dict] = None,
            log_failure: bool = True) -> bool:
        try:
            if self.store.has_timeline_payload(game_id, "lcu_timeline"):
                return True
        except (sqlite3.Error, OSError) as exc:
            if log_failure:
                self.on_log(
                    f"History could not inspect timeline {game_id}: {exc}",
                    "warn",
                )
            return False
        try:
            payload = self._lcu_timeline_provider.fetch_match_timeline(
                game_id, match_payload=match_payload,
            )
        except TimelineProviderError as exc:
            try:
                self.store.record_timeline_fetch_failure(
                    game_id, "lcu_timeline", type(exc).__name__,
                )
            except (sqlite3.Error, OSError) as state_exc:
                if log_failure:
                    self.on_log(
                        "History could not record timeline retry state "
                        f"for {game_id}: {state_exc}",
                        "warn",
                    )
            if log_failure:
                self.on_log(
                    f"History could not capture timeline {game_id}: {exc}",
                    "warn",
                )
            return False
        try:
            self.store.save_timeline_payload(
                game_id,
                payload.source,
                {
                    "provenance": asdict(payload.provenance),
                    "timeline": payload.timeline,
                },
                schema_version="lcu-v1",
                completeness=payload.completeness,
            )
            self.store.clear_timeline_fetch_failure(game_id, payload.source)
        except (sqlite3.Error, OSError) as exc:
            if log_failure:
                self.on_log(
                    f"History could not persist timeline {game_id}: {exc}",
                    "warn",
                )
            return False
        self.refresh_score_v2(game_id, log_failure=log_failure)
        return True

    def _save_match_v5_timeline(
            self, game_id: int, game_payload: Optional[dict] = None,
            log_failure: bool = True) -> bool:
        with self._match_v5_fetch_lock:
            return self._save_match_v5_timeline_unlocked(
                game_id, game_payload, log_failure,
            )

    def _record_match_v5_failure(
            self, game_id: int, error_kind: str,
            log_failure: bool) -> None:
        try:
            self.store.record_timeline_fetch_failure(
                game_id, MATCH_V5_SOURCE, error_kind,
            )
        except (sqlite3.Error, OSError) as state_exc:
            if log_failure:
                self.on_log(
                    "History could not record Match-V5 retry state "
                    f"for {game_id}: {state_exc}",
                    "warn",
                )

    def _save_match_v5_timeline_unlocked(
            self, game_id: int, game_payload: Optional[dict] = None,
            log_failure: bool = True) -> bool:
        """Best-effort, opt-in Match-V5 timeline upgrade.

        Always runs strictly after LCU-backed history for this game is
        already durable, and every failure mode here (feature disabled, no
        usable key, auth rejected, rate limited, upstream error, payload
        validation, cross-source mismatch) is caught locally and reduced to
        a bool plus sanitized status/backoff state -- never raised, so it
        can never delay or break LCU-derived history.
        """
        if not is_private_match_v5_enabled():
            return False

        tracker = self._riot_status_tracker
        secret_store = tracker.store
        if secret_store is None:
            return False
        # Check key availability before touching cache/backoff state or
        # the network: a missing/corrupt key is a global configuration
        # issue, not a per-match failure, so it must not create backoff
        # rows for every match in the library.
        try:
            if secret_store.status() is not SecretStoreStatus.AVAILABLE:
                return False
        except OSError:
            return False

        try:
            if self.store.has_timeline_payload(game_id, MATCH_V5_SOURCE):
                return True
            if not self.store.timeline_fetch_due(game_id, MATCH_V5_SOURCE):
                return False
        except (sqlite3.Error, OSError) as exc:
            if log_failure:
                self.on_log(
                    "History could not inspect Match-V5 timeline state "
                    f"for {game_id}: {exc}",
                    "warn",
                )
            return False

        try:
            platform_id = platform_id_from_lcu_match(
                game_payload if game_payload is not None
                else self.lcu.get_match_details(game_id)
            )
            match_id = build_match_v5_id(platform_id, game_id)
            client = self._riot_client_factory(secret_store.get_key)
            provider = RiotMatchV5Provider(client, status_recorder=tracker)
            payload = provider.fetch_match_timeline(match_id)
            stored_puuids = self.store.participant_puuids(game_id)
            fetched_puuids = {
                participant.get("puuid")
                for participant in (payload.match.get("info") or {}).get(
                    "participants", []
                )
                if isinstance(participant, dict) and participant.get("puuid")
            }
            if stored_puuids and stored_puuids != fetched_puuids:
                raise TimelineProviderValidationError(
                    f"Riot Match-V5 participants for {match_id} did not "
                    "match the locally stored match; refusing to store a "
                    "cross-source mismatch."
                )
        except (sqlite3.Error, OSError) as exc:
            self._record_match_v5_failure(
                game_id, type(exc).__name__, log_failure,
            )
            if log_failure:
                self.on_log(
                    "History could not validate Match-V5 participant "
                    f"identities for {game_id}: {exc}",
                    "warn",
                )
            return False
        except TimelineProviderDisabledError:
            return False
        except (ValueError, LCUConnectionError, TimelineProviderError,
                RiotApiError, SecretStoreError) as exc:
            error_kind = type(exc).__name__
            self._record_match_v5_failure(
                game_id, error_kind, log_failure,
            )
            if log_failure:
                self.on_log(
                    f"History could not upgrade timeline {game_id} via "
                    f"Match-V5 (status: {tracker.status().value}).",
                    "info",
                )
            return False

        try:
            self.store.save_timeline_payload(
                game_id,
                payload.source,
                {
                    "provenance": asdict(payload.provenance),
                    "match": payload.match,
                    "timeline": payload.timeline,
                },
                schema_version="match-v5-v1",
                completeness=payload.completeness,
            )
            self.store.clear_timeline_fetch_failure(game_id, payload.source)
        except (sqlite3.Error, OSError) as exc:
            self._record_match_v5_failure(
                game_id, type(exc).__name__, log_failure,
            )
            if log_failure:
                self.on_log(
                    f"History could not persist Match-V5 timeline {game_id}: {exc}",
                    "warn",
                )
            return False
        self.refresh_score_v2(game_id, log_failure=log_failure)
        return True

    def sync_recent(self, limit: int = 100) -> int:
        if not self._sync_lock.acquire(blocking=False):
            return 0
        imported = 0
        timeline_failures = 0
        timeline_attempted = set()
        timeline_scan_failed = False
        match_v5_failures = 0
        match_v5_scan_failed = False
        try:
            known = self.store.known_game_ids()
            summaries = self.lcu.get_match_history_summaries(limit)
            for summary in summaries:
                game_id = summary.get("gameId")
                if not isinstance(game_id, int) or game_id <= 0:
                    continue
                if game_id in known:
                    continue
                if summary.get("queueId") not in SUPPORTED_QUEUE_IDS \
                        or summary.get("mapId") != SUMMONERS_RIFT_MAP_ID:
                    continue
                try:
                    if self.ingest_game(
                            game_id, notify=False,
                            log_timeline_failure=False,
                            attempt_match_v5=False):
                        imported += 1
                        timeline_attempted.add(game_id)
                        if not self.store.has_timeline_payload(
                                game_id, "lcu_timeline"):
                            timeline_failures += 1
                except (LCUConnectionError, RuntimeError, ValueError, OSError) as e:
                    self.on_log(f"History could not import game {game_id}: {e}", "warn")
            try:
                missing_timeline_ids = self.store.game_ids_missing_timeline(
                    "lcu_timeline", limit,
                )
            except (sqlite3.Error, OSError):
                missing_timeline_ids = []
                timeline_scan_failed = True
            for game_id in missing_timeline_ids:
                if game_id in timeline_attempted:
                    continue
                try:
                    saved = self._save_lcu_timeline(
                        game_id, log_failure=False,
                    )
                except (sqlite3.Error, OSError, RuntimeError, ValueError):
                    saved = False
                if not saved:
                    timeline_failures += 1
            match_v5_failures, match_v5_scan_failed = (
                self._backfill_match_v5_timelines(limit)
            )
            self.store.set_meta("initial_backfill_complete", "1")
            self.store.set_meta(
                "last_sync", datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
            if timeline_failures:
                self.on_log(
                    "History could not capture "
                    f"{timeline_failures} local timeline"
                    f"{'s' if timeline_failures != 1 else ''}; "
                    "aggregate evidence remains available.",
                    "warn",
                )
            if timeline_scan_failed:
                self.on_log(
                    "History could not inspect missing local timelines; "
                    "timeline backfill will retry later.",
                    "warn",
                )
            if match_v5_failures:
                self.on_log(
                    "History could not upgrade "
                    f"{match_v5_failures} timeline"
                    f"{'s' if match_v5_failures != 1 else ''} via Match-V5; "
                    "LCU-derived evidence remains available.",
                    "info",
                )
            if match_v5_scan_failed:
                self.on_log(
                    "History could not inspect missing Match-V5 timelines; "
                    "upgrade backfill will retry later.",
                    "info",
                )
            if imported and self.on_updated:
                self.on_updated()
            return imported
        finally:
            self._sync_lock.release()

    def _backfill_match_v5_timelines(self, limit: int) -> tuple[int, bool]:
        """Opportunistically upgrade already-known games missing a Match-V5
        timeline. Purely additive to LCU-derived history: returns
        (failure_count, scan_failed) and never raises, so a disabled
        feature, missing key, or upstream outage cannot affect the rest of
        sync_recent.
        """
        if not is_private_match_v5_enabled():
            return 0, False
        tracker = self._riot_status_tracker
        secret_store = tracker.store
        if secret_store is None:
            return 0, False
        try:
            if secret_store.status() is not SecretStoreStatus.AVAILABLE:
                return 0, False
        except OSError:
            return 0, False

        failures = 0
        try:
            missing_ids = self.store.game_ids_missing_timeline(
                MATCH_V5_SOURCE, limit,
            )
        except (sqlite3.Error, OSError):
            return 0, True
        for game_id in missing_ids:
            try:
                saved = self._save_match_v5_timeline(game_id, log_failure=False)
            except (sqlite3.Error, OSError, RuntimeError, ValueError):
                saved = False
            if not saved:
                failures += 1
                # A rate-limit response applies to the whole key, not just
                # this match: stop this pass rather than immediately
                # hammering the next game and risking a retry storm. Each
                # skipped game keeps its own backoff schedule via
                # timeline_fetch_attempts, so this does not starve them --
                # they are simply retried on a later sync_recent pass.
                if tracker.status() in {
                        ProviderStatus.AUTH_REJECTED,
                        ProviderStatus.RATE_LIMITED,
                        ProviderStatus.UPSTREAM_UNAVAILABLE,
                }:
                    break
        return failures, False

    def ingest_after_game(self, retries: int = 30, delay: float = 2.0) -> Optional[int]:
        with self._state_lock:
            game_id = self._active_game_id
            positions = dict(self._active_positions)
            self._active_game_id = None
            self._active_positions = {}
        if game_id and self.store.has_game(game_id):
            game_id = None
        for attempt in range(retries):
            if not game_id:
                eog = self.lcu.get_end_of_game_stats() or {}
                candidate = eog.get("gameId")
                if isinstance(candidate, int) and candidate > 0 \
                        and not self.store.has_game(candidate):
                    game_id = candidate
            if not game_id:
                summaries = self.lcu.get_match_history_summaries(1)
                candidate = summaries[0].get("gameId") if summaries else None
                if isinstance(candidate, int) and candidate > 0 \
                        and not self.store.has_game(candidate):
                    game_id = candidate
            if game_id:
                try:
                    report = self.ingest_game(
                        game_id, captured_positions=positions, notify=True,
                    )
                    if report:
                        if self.on_postgame:
                            self.on_postgame(game_id)
                        return game_id
                    return None
                except Exception as e:
                    if attempt == retries - 1:
                        raise RuntimeError(
                            f"Post-game import failed for {game_id}: {e}"
                        ) from e
            time.sleep(delay)
        return None

    def summary(self) -> dict:
        return self.store.get_summary()

    def list_history(self, offset: int = 0, limit: int = 25) -> list[dict]:
        return self.store.list_history(offset, limit)

    def report(self, game_id: int) -> Optional[dict]:
        return self.store.get_report(game_id)
