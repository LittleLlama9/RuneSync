"""Ingest AngryBacteria Match-V5/Timeline-V5 dumps into a DAEMON Score v2 corpus.

The AngryBacteria Hugging Face dataset
(https://huggingface.co/datasets/AngryBacteria/league_of_legends) is the only
public *non-professional* League corpus that carries full Riot Timeline-V5
per-minute participant frames (XY position, gold, XP) and events -- exactly the
evidence tier ``score_features.py`` consumes. It is used here **privately, for a
single-user research beta**, not as a shipped dependency: the raw dump is
third-party-redistributed Riot data whose Apache-2.0 tag cannot grant rights
over Riot's underlying match records (see the vault decision
"AngryBacteria is right-shape but license-questionable"). We therefore:

- never persist a raw Riot PUUID, ``riotIdGameName``, ``summonerName`` or
  ``riotIdTagline`` -- player identity is reduced to a salted SHA-256 group key
  that is stable across matches (so player-disjoint splitting still works) but
  is not reversible to a Riot identity;
- keep only Summoner's Rift 5v5 queues (ranked solo 420, normal draft 400,
  ranked flex 440), stamping the queue as domain metadata so flex/draft can be
  ablated later, and drop ARAM/Arena/URF/PvE which DAEMON Score was never built
  to grade;
- refuse to guess missing structure (a game without exactly five canonical team
  positions per side, or without 10 players, or a remake) rather than fabricate
  roles.

This module is pure and streaming: it never opens a network connection and
parses the multi-gigabyte dumps with :mod:`ijson` so memory stays bounded.
``score_match`` (the tested v1 scorer) is reused so each ingested game has a v1
score run -- that is what makes ``HistoryStore.get_report`` (and therefore the
corpus manifest builder) work, and it doubles as the v1 baseline for shadow
comparison.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Iterator, Optional

from performance_score import SCORING_MODEL_VERSION, score_match

# Summoner's Rift 5v5 queues DAEMON Score is designed for. All three share the
# same map, objectives, roles and Timeline-V5 shape, so the per-match
# performance signal is computed identically; the queue is retained only as
# ablatable domain metadata, never as a training target.
POOLED_QUEUE_IDS = {420, 400, 440}
QUEUE_DOMAIN = {
    420: "ranked_solo",
    400: "normal_draft",
    440: "ranked_flex",
}
SUMMONERS_RIFT_MAP_ID = 11
MIN_DURATION_SECONDS = 300

# Match-V5 ``teamPosition`` -> RuneSync role vocabulary.
ROLE_BY_POSITION = {
    "TOP": "top",
    "JUNGLE": "jungle",
    "MIDDLE": "mid",
    "BOTTOM": "bot",
    "UTILITY": "support",
}
_CANONICAL_POSITIONS = frozenset(ROLE_BY_POSITION)

TIMELINE_SOURCE = "match_v5"
_IDENTITY_KEYS_NEVER_STORED = (
    "puuid", "summonerName", "riotIdGameName", "riotIdTagline", "summonerId",
)


class SkipMatch(Exception):
    """A match is not eligible for the Rift-5v5 corpus and must be skipped.

    Raising (rather than silently returning ``None``) keeps the ingest loop's
    accounting honest: every skip carries a machine-readable reason that is
    tallied, so a surprising drop rate is visible instead of hidden.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _num(value, default: Optional[int] = 0) -> Optional[int]:
    """Unwrap a MongoDB extended-JSON number or pass a plain number through.

    The dump mixes plain ints (``queueId``) with wrapped longs
    (``{"$numberLong": "1724067103756"}``); both must read as Python ints.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        for key in ("$numberLong", "$numberInt", "$numberDouble"):
            if key in value:
                try:
                    return int(float(value[key]))
                except (TypeError, ValueError):
                    return default
    return default


def player_group_key(puuid: str, salt: bytes) -> str:
    """Stable, non-reversible per-player key for split grouping.

    Salting with a locally generated secret means the key cannot be matched
    back to a Riot PUUID by anyone who does not hold the salt, while remaining
    identical for the same player across matches so the strict player-disjoint
    splitter can keep one player in one split.
    """
    if not puuid:
        raise SkipMatch("participant_missing_puuid")
    return hashlib.sha256(salt + puuid.encode("utf-8")).hexdigest()


def _match_id_of(document: dict) -> Optional[str]:
    return ((document.get("metadata") or {}).get("matchId")) or None


def build_report(info: dict, salt: bytes) -> dict:
    """Convert one Match-V5 ``info`` block into a RuneSync report dict.

    Raises :class:`SkipMatch` for anything outside the Rift-5v5 pool or missing
    the structure DAEMON Score requires. The returned dict is exactly the shape
    :meth:`HistoryStore.save_report` consumes (``match``/``participants``/
    ``scores``), with identities reduced to salted group keys.
    """
    queue_id = _num(info.get("queueId"), None)
    map_id = _num(info.get("mapId"), None)
    if queue_id not in POOLED_QUEUE_IDS:
        raise SkipMatch(f"queue_not_pooled:{queue_id}")
    if map_id != SUMMONERS_RIFT_MAP_ID:
        raise SkipMatch(f"map_not_rift:{map_id}")

    raw_participants = info.get("participants") or []
    if len(raw_participants) != 10:
        raise SkipMatch(f"participant_count:{len(raw_participants)}")

    duration = _num(info.get("gameDuration"), 0) or 0
    if duration < MIN_DURATION_SECONDS:
        raise SkipMatch(f"too_short:{duration}")
    if any(
            bool(p.get("gameEndedInEarlySurrender"))
            or bool(p.get("teamEarlySurrendered"))
            for p in raw_participants):
        raise SkipMatch("early_surrender")

    players: list[dict] = []
    positions_by_team: dict[int, set[str]] = {}
    for participant in raw_participants:
        position = str(participant.get("teamPosition") or "").upper()
        if position not in _CANONICAL_POSITIONS:
            raise SkipMatch(f"non_canonical_position:{position or 'empty'}")
        team_id = _num(participant.get("teamId"), 0)
        positions_by_team.setdefault(team_id, set()).add(position)

        pid = _num(participant.get("participantId"), 0)
        group_key = player_group_key(str(participant.get("puuid") or ""), salt)
        players.append({
            "participant_id": pid,
            # The salted group key occupies the ``puuid`` column so the
            # existing splitter (which groups on stored ``puuid``) keeps one
            # player in one split, without ever persisting a real Riot PUUID.
            "puuid": group_key,
            "summoner_name": f"Player {pid}",
            "champion_id": _num(participant.get("championId"), 0),
            "champion_name": str(participant.get("championName") or "unknown"),
            "team_id": team_id,
            "role": ROLE_BY_POSITION[position],
            "win": bool(participant.get("win")),
            "kills": _num(participant.get("kills"), 0),
            "deaths": _num(participant.get("deaths"), 0),
            "assists": _num(participant.get("assists"), 0),
            "gold_earned": _num(participant.get("goldEarned"), 0),
            "cs": _num(participant.get("totalMinionsKilled"), 0)
                  + _num(participant.get("neutralMinionsKilled"), 0),
            "champion_level": _num(participant.get("champLevel"), 0),
            "damage_to_champions": _num(
                participant.get("totalDamageDealtToChampions"), 0),
            "damage_to_objectives": _num(
                participant.get("damageDealtToObjectives"), 0),
            "damage_to_turrets": _num(participant.get("damageDealtToTurrets"), 0),
            "damage_taken": _num(participant.get("totalDamageTaken"), 0),
            "damage_mitigated": _num(participant.get("damageSelfMitigated"), 0),
            "healing": _num(participant.get("totalHeal"), 0),
            "vision_score": _num(participant.get("visionScore"), 0),
            "wards_placed": _num(participant.get("wardsPlaced"), 0),
            "wards_killed": _num(participant.get("wardsKilled"), 0),
            "items": [
                item_id for item_id in (
                    _num(participant.get(f"item{slot}"), 0) for slot in range(7)
                ) if item_id and item_id > 0
            ],
        })

    for team_id, seen in positions_by_team.items():
        if seen != _CANONICAL_POSITIONS:
            raise SkipMatch(f"incomplete_roles_team_{team_id}")
    if len(positions_by_team) != 2:
        raise SkipMatch(f"team_count:{len(positions_by_team)}")

    participant_ids = [p["participant_id"] for p in players]
    if len(set(participant_ids)) != 10:
        raise SkipMatch("duplicate_participant_ids")

    game_id = _num(info.get("gameId"), 0) or 0
    if game_id <= 0:
        raise SkipMatch("invalid_game_id")

    scores = score_match(players, duration)

    # There is no "local" player in an amateur corpus game; participant 1 is a
    # deterministic nominal anchor used only for match-level metadata columns.
    # No supervision or panel label depends on which player is nominal.
    local = next(p for p in players if p["participant_id"] == min(participant_ids))
    creation = _num(info.get("gameCreation"), 0) or 0

    return {
        "match": {
            "game_id": game_id,
            "queue_id": queue_id,
            "map_id": map_id,
            "game_mode": str(info.get("gameMode") or "CLASSIC"),
            "game_creation": creation,
            "game_creation_date": _iso_from_ms(creation),
            "duration": duration,
            "patch": str(info.get("gameVersion") or ""),
            "local_participant_id": local["participant_id"],
            "local_win": local["win"],
            "local_champion_id": local["champion_id"],
            "local_champion_name": local["champion_name"],
            "local_role": local["role"],
            "score_model_version": SCORING_MODEL_VERSION,
        },
        "participants": players,
        "scores": scores,
    }


def _iso_from_ms(ms: int) -> str:
    import datetime

    if ms <= 0:
        return ""
    seconds = ms / 1000 if ms > 10_000_000_000 else ms
    return datetime.datetime.fromtimestamp(
        seconds, tz=datetime.timezone.utc,
    ).isoformat()


_NUMBER_WRAPPER_KEYS = ("$numberLong", "$numberInt", "$numberDouble",
                        "$numberDecimal")


def _deep_unwrap(value):
    """Recursively convert MongoDB extended-JSON numbers to Python numbers.

    ``$numberInt``/``$numberLong`` become ``int``; ``$numberDouble``/
    ``$numberDecimal`` become ``float`` (precision preserved, unlike ``_num``
    which truncates). Plain values and already-unwrapped numbers pass through
    unchanged, so a dump that stores raw ints is a no-op. Dicts and lists are
    walked so nested event timestamps and participant-frame numerics
    (``totalGold``, ``position.x/y``, ...) are unwrapped too -- otherwise a
    wrapped nested number would survive as a ``dict`` and be silently dropped
    by ``score_features.canonical_timeline_events`` (its ``isinstance`` guard),
    gutting the fight/objective/vision/death evidence for the whole game.
    """
    if isinstance(value, dict):
        if len(value) == 1:
            (only_key, only_val), = value.items()
            if only_key in ("$numberInt", "$numberLong"):
                try:
                    return int(float(only_val))
                except (TypeError, ValueError):
                    return value
            if only_key in ("$numberDouble", "$numberDecimal"):
                try:
                    return float(only_val)
                except (TypeError, ValueError):
                    return value
        return {key: _deep_unwrap(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_unwrap(item) for item in value]
    return value


def build_timeline_payload(timeline_info: dict) -> dict:
    """Wrap a Timeline-V5 ``info`` block in the stored-payload shape.

    ``score_features.canonical_timeline_*`` read ``payload["timeline"]["frames"]``
    with plain-int ``timestamp``/``position``/participant-frame fields. The
    dump stores the feature-relevant nested numbers as raw ints, but some
    fields (e.g. an event's ``realTimestamp``) ship as extended-JSON wrappers,
    and a differently-encoded dump could wrap the numbers we depend on. Each
    frame is therefore deep-unwrapped so no wrapped nested number can survive
    to be silently discarded downstream.
    """
    frames = timeline_info.get("frames") or []
    normalized = []
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        item = _deep_unwrap(frame)
        item["timestamp"] = _num(frame.get("timestamp"), 0)
        normalized.append(item)
    return {"timeline": {"frames": normalized}}


def stream_documents(path: str) -> Iterator[dict]:
    """Yield each top-level document from a BSON-JSON array file, streaming."""
    import ijson

    with open(path, "rb") as handle:
        yield from ijson.items(handle, "item")


def assert_no_identity_leak(report: dict) -> None:
    """Fail loudly if any raw Riot identity survived into a report to persist.

    A salted 64-hex group key is allowed in the ``puuid`` column; a raw Riot
    PUUID (typically ~78 chars, non-hex alphabet) or any name field is not.
    """
    for player in report.get("participants") or []:
        key = str(player.get("puuid") or "")
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("Un-hashed identity reached a persisted report")
        name = str(player.get("summoner_name") or "")
        if not name.startswith("Player "):
            raise ValueError("Real summoner name reached a persisted report")
