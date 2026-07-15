"""Post-game match normalization, backfill, and ingestion orchestration."""

import datetime
import threading
import time
from itertools import permutations
from typing import Callable, Optional

from champion_roles import get_role_weights
from history_store import HistoryStore
from lcu import LCUConnectionError
from performance_score import SCORING_MODEL_VERSION, score_match


SUPPORTED_QUEUE_IDS = {400, 420, 430, 440, 480, 490}
SUMMONERS_RIFT_MAP_ID = 11
TEAM_ROLES = ("top", "jungle", "mid", "bot", "support")


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
            on_postgame: Optional[Callable[[int], None]] = None):
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

    def _ensure_champions(self) -> dict[int, str]:
        if not self._champion_names:
            self._champion_names = self.lcu.get_champion_name_map()
        return self._champion_names

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
            notify: bool = True) -> Optional[dict]:
        with self._ingest_lock:
            if self.store.has_game(game_id):
                return self.store.get_report(game_id)
            local_puuid = self.lcu.get_current_summoner_puuid()
            if not local_puuid:
                raise RuntimeError("Current summoner PUUID is unavailable")
            game = self.lcu.get_match_details(game_id)
            try:
                report = normalize_match(
                    game, local_puuid, self._ensure_champions(), captured_positions,
                )
            except UnsupportedMatch as e:
                self.on_log(f"History skipped game {game_id}: {e}", "info")
                return None
            self.store.save_report(report)
            if notify and self.on_updated:
                self.on_updated()
            return report

    def sync_recent(self, limit: int = 100) -> int:
        if not self._sync_lock.acquire(blocking=False):
            return 0
        imported = 0
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
                    if self.ingest_game(game_id, notify=False):
                        imported += 1
                except (LCUConnectionError, RuntimeError, ValueError, OSError) as e:
                    self.on_log(f"History could not import game {game_id}: {e}", "warn")
            self.store.set_meta("initial_backfill_complete", "1")
            self.store.set_meta(
                "last_sync", datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
            if imported and self.on_updated:
                self.on_updated()
            return imported
        finally:
            self._sync_lock.release()

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
