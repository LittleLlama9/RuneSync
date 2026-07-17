"""Source-aware DAEMON Score v2 feature/evidence extraction.

This module turns whatever timeline/aggregate evidence RuneSync happened to
capture for a match into a stable, source-labeled set of per-participant
features, without training or applying any model and without ever reading a
win/result flag. It sits strictly between the four-tier evidence hierarchy
(see the vault decision "Promote LCU post-game timelines into DAEMON Score
v2 evidence hierarchy") and `HistoryStore.save_feature_set`:

    Match-V5 timeline > LCU post-game timeline > Live Client capture > aggregate

Design rules that the tests in tests/test_score_features.py enforce:

  * Capability-honest: `EvidenceCapabilities` records which tiers are
    actually available for a game. A feature family that needs evidence a
    tier does not carry (e.g. ward/item events, which the verified LCU
    historical timeline omits -- see the vault capability note "LCU
    historical game timeline endpoint") is always returned as an explicit
    `{"available": False, "reason": ...}` block, never silently computed as
    zero or assumed complete.
  * Influence, not raw stats: `fight_influence`, `objective_participation`,
    `structure_pressure`, `enablement_suppression`, and `vision_influence`
    are derived ONLY from timestamped, participant-anchored events (who
    secured/assisted/contested what, and when). Aggregate stats like
    `damage_to_turrets` or `vision_score` are exposed only in the separate
    `raw` block for provenance and must never leak into an "influence"
    field -- see `test_velkoz_turret_damage_is_not_objective_credit` and
    `test_seraphine_raw_vision_does_not_become_influence`.
  * No outcome leakage: nothing in this module ever reads a `win` or
    `local_win` field (participant rows are stripped of them on entry), and
    `phase_breakdown` buckets are each computed from an independently
    time-bounded event slice so a later phase can never influence an
    earlier one.
  * Deterministic and bounded: every function here is pure (no randomness,
    no wall-clock reads), keyed by participant ID (not list position) so
    output is invariant to input participant order, and every rate-like
    output is clamped/rounded so repeated runs hash identically through
    `HistoryStore.save_feature_set`'s content hashing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

FEATURE_VERSION = "2.0.0-evidence"

# Evidence source identifiers. match_v5/lcu_timeline match the `source`
# values timeline_provider.py already writes to `timeline_payloads.source`;
# live_client/aggregate are new labels for the remaining two evidence tiers.
MATCH_V5 = "match_v5"
LCU_TIMELINE = "lcu_timeline"
LIVE_CLIENT = "live_client"
AGGREGATE = "aggregate"
SOURCE_PRIORITY = (MATCH_V5, LCU_TIMELINE, LIVE_CLIENT, AGGREGATE)

# Below this, a single lane skirmish or early surrender can dominate every
# percentile/derived signal -- see the Sion 8:30 (510s) OP.GG divergence
# case. Feature sets for shorter games are still produced (evidence is
# evidence) but flagged `abstain` so no consumer treats them as a normal
# confident vector.
SHORT_GAME_ABSTAIN_SECONDS = 600

# Standard League phase boundaries in milliseconds. Used only to bucket
# ALREADY-KNOWN event timestamps into non-overlapping windows -- never to
# look past a boundary while computing an earlier phase.
PHASE_BOUNDARIES_MS = (
    ("early", 0.0, 14 * 60_000.0),
    ("mid", 14 * 60_000.0, 25 * 60_000.0),
    ("late", 25 * 60_000.0, math.inf),
)

# Riot's Match-V5/LCU timeline `monsterType` values. Voidgrubs are "HORDE".
GRUB_MONSTER_TYPES = frozenset({"HORDE"})
EPIC_MONSTER_TYPES = frozenset({"DRAGON", "BARON_NASHOR", "RIFTHERALD", "ATAKHAN"})

TRADE_WINDOW_MS = 10_000.0
RAPID_DEATH_WINDOW_MS = 45_000.0
VISION_ACTIONABLE_WINDOW_MS = 20_000.0
OBJECTIVE_FIGHT_WINDOW_MS = 20_000.0
GOLD_LEAD_THRESHOLD = 300.0
CONVERSION_WINDOW_MS = 60_000.0
SPLIT_ISOLATION_DISTANCE = 3000.0
CAUSAL_EVENT_DISTANCE = 3000.0
FRAME_POSITION_MAX_AGE_MS = 60_000.0

_LANE_ROLES = frozenset({"top", "jungle", "mid", "bot", "support"})


# ── capability detection ────────────────────────────────────────────────────

@dataclass(frozen=True)
class EvidenceCapabilities:
    """Which of the four DAEMON Score v2 evidence tiers exist for a game.

    `aggregate` is the only tier that is always True once a match has been
    ingested at all -- the other three are explicit, individually verified
    capability flags, never inferred from each other. A missing tier must
    never be treated as "zero evidence" (a feature is 0) nor as "full
    evidence" (silently substitute a weaker tier's data as if it were as
    complete as a richer one); see `best_source`.
    """

    match_v5: bool = False
    lcu_timeline: bool = False
    live_client: bool = False
    aggregate: bool = True
    match_v5_completeness: Optional[float] = None
    lcu_timeline_completeness: Optional[float] = None
    live_client_completeness: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "match_v5": self.match_v5,
            "lcu_timeline": self.lcu_timeline,
            "live_client": self.live_client,
            "aggregate": self.aggregate,
        }

    def best_source(self) -> str:
        candidates = [
            source for source in SOURCE_PRIORITY
            if source == AGGREGATE or getattr(self, source)
        ]
        priority = {source: -index for index, source in enumerate(SOURCE_PRIORITY)}
        return max(
            candidates,
            key=lambda source: (
                -1.0 if source == AGGREGATE else self.source_completeness(source),
                priority[source],
            ),
        )

    def has_event_timeline(self) -> bool:
        return self.match_v5 or self.lcu_timeline

    def source_completeness(self, source: str) -> float:
        if source == AGGREGATE:
            return 1.0 if self.aggregate else 0.0
        value = getattr(self, f"{source}_completeness")
        if value is not None:
            return max(0.0, min(1.0, float(value)))
        return 1.0 if getattr(self, source) else 0.0

    def quality_dict(self) -> dict:
        return {
            source: self.source_completeness(source)
            for source in SOURCE_PRIORITY
        }


def _stored_timeline_capability(store, game_id: int, source: str) -> tuple[bool, float]:
    getter = getattr(store, "get_timeline_payload", None)
    if getter is not None:
        stored = getter(game_id, source)
        if not stored:
            return False, 0.0
        completeness = max(0.0, min(1.0, float(stored.get("completeness", 1.0))))
        return completeness > 0.0, completeness
    available = bool(store.has_timeline_payload(game_id, source))
    return available, 1.0 if available else 0.0


def _eligible_live_sessions(store, game_id: int) -> list[dict]:
    eligible = []
    for session in store.list_live_capture_sessions():
        status = str(session.get("status") or "")
        completeness = max(
            0.0, min(1.0, float(session.get("completeness") or 0.0)),
        )
        if (
            session.get("game_id") == game_id
            and (status == "completed" or status.startswith("partial_"))
            and completeness > 0.0
        ):
            eligible.append(session)
    return eligible


def _best_live_session(store, game_id: int) -> Optional[dict]:
    sessions = _eligible_live_sessions(store, game_id)
    if not sessions:
        return None
    return max(
        sessions,
        key=lambda session: (
            float(session.get("completeness") or 0.0),
            str(session.get("updated_at") or session.get("ended_at") or ""),
            str(session.get("session_id") or ""),
        ),
    )


def detect_capabilities(store, game_id: int) -> EvidenceCapabilities:
    """Inspect `store` for which evidence tiers exist for `game_id`.

    Only reads existing HistoryStore accessors (`has_timeline_payload`,
    `list_live_capture_sessions`, `has_game`); never fetches anything over
    the network and never assumes a tier is present without checking.
    """
    match_v5, match_v5_completeness = _stored_timeline_capability(
        store, game_id, MATCH_V5,
    )
    lcu_timeline, lcu_timeline_completeness = _stored_timeline_capability(
        store, game_id, LCU_TIMELINE,
    )
    live_session = _best_live_session(store, game_id)
    live_client = live_session is not None
    live_client_completeness = (
        float(live_session.get("completeness") or 0.0)
        if live_session is not None else 0.0
    )
    aggregate = bool(store.has_game(game_id))
    return EvidenceCapabilities(
        match_v5=match_v5, lcu_timeline=lcu_timeline,
        live_client=live_client, aggregate=aggregate,
        match_v5_completeness=match_v5_completeness,
        lcu_timeline_completeness=lcu_timeline_completeness,
        live_client_completeness=live_client_completeness,
    )


# ── canonical event/frame normalization ─────────────────────────────────────

def _phase_of(t_ms: float) -> str:
    for name, start, end in PHASE_BOUNDARIES_MS:
        if start <= t_ms < end:
            return name
    return "late"


def canonical_timeline_events(frames: list, source: str) -> list[dict]:
    """Normalize LCU/Match-V5 timeline frame events to a common event shape.

    Both providers validate against the same frame schema (see
    `timeline_provider._validate_timeline_frames`): a list of frames each
    carrying `timestamp`, `participantFrames`, and `events`. Event kinds
    RuneSync does not model (item purchases, skill levels, ...) are dropped
    rather than guessed at.
    """
    events: list[dict] = []
    for frame in frames or []:
        if not isinstance(frame, dict):
            continue
        for raw in frame.get("events") or []:
            if not isinstance(raw, dict):
                continue
            t_ms = raw.get("timestamp")
            if not isinstance(t_ms, (int, float)):
                continue
            t_ms = float(t_ms)
            etype = raw.get("type")
            canonical: Optional[dict] = None
            if etype == "CHAMPION_KILL":
                canonical = {
                    "kind": "champion_kill",
                    "killer": raw.get("killerId") or None,
                    "victim": raw.get("victimId"),
                    "assists": list(raw.get("assistingParticipantIds") or []),
                    "position": raw.get("position"),
                }
            elif etype == "BUILDING_KILL":
                canonical = {
                    "kind": "building_kill",
                    "killer": raw.get("killerId") or None,
                    "assists": list(raw.get("assistingParticipantIds") or []),
                    "team_destroyed": raw.get("teamId"),
                    "building_type": raw.get("buildingType"),
                    "tower_type": raw.get("towerType"),
                    "lane": raw.get("laneType"),
                    "position": raw.get("position"),
                }
            elif etype == "TURRET_PLATE_DESTROYED":
                canonical = {
                    "kind": "turret_plate",
                    "killer": raw.get("killerId"),
                    "assists": [],
                    "team_destroyed": raw.get("teamId"),
                    "lane": raw.get("laneType"),
                }
            elif etype == "ELITE_MONSTER_KILL":
                canonical = {
                    "kind": "elite_monster_kill",
                    "killer": raw.get("killerId"),
                    "killer_team": raw.get("killerTeamId"),
                    "assists": list(raw.get("assistingParticipantIds") or []),
                    "monster_type": raw.get("monsterType"),
                    "monster_sub_type": raw.get("monsterSubType"),
                    "position": raw.get("position"),
                }
            elif etype in ("WARD_PLACED_EVENT", "WARD_PLACED"):
                canonical = {
                    "kind": "ward_placed",
                    "actor": raw.get("creatorId") or raw.get("killerId"),
                    "ward_type": raw.get("wardType"),
                    "position": raw.get("position"),
                }
            elif etype in ("WARD_KILL_EVENT", "WARD_KILL"):
                canonical = {
                    "kind": "ward_kill",
                    "actor": raw.get("killerId"),
                    "ward_type": raw.get("wardType"),
                    "position": raw.get("position"),
                }
            if canonical is not None:
                canonical["t_ms"] = t_ms
                canonical["phase"] = _phase_of(t_ms)
                canonical["source"] = source
                events.append(canonical)
    events.sort(key=lambda e: e["t_ms"])
    return events


def canonical_timeline_frames(frames: list) -> dict[int, list[dict]]:
    """Return `{participant_id: [{t_ms, gold, xp, level, cs, position}, ...]}`."""
    per_participant: dict[int, list[dict]] = {}
    for frame in frames or []:
        if not isinstance(frame, dict):
            continue
        t_ms = frame.get("timestamp")
        if not isinstance(t_ms, (int, float)):
            continue
        t_ms = float(t_ms)
        for key, pframe in (frame.get("participantFrames") or {}).items():
            try:
                pid = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(pframe, dict):
                continue
            gold = pframe.get("totalGold")
            if gold is None:
                gold = pframe.get("currentGold")
            position = pframe.get("position")
            per_participant.setdefault(pid, []).append({
                "t_ms": t_ms,
                "gold": gold,
                "xp": pframe.get("xp"),
                "level": pframe.get("level"),
                "cs": pframe.get("minionsKilled"),
                "jungle_cs": pframe.get("jungleMinionsKilled"),
                "position": position if isinstance(position, dict) else None,
            })
    for pid in per_participant:
        per_participant[pid].sort(key=lambda sample: sample["t_ms"])
    return per_participant


_LIVE_CLIENT_MONSTER_TYPES = {
    "DragonKill": "DRAGON", "HeraldKill": "RIFTHERALD", "BaronKill": "BARON_NASHOR",
    "HordeKill": "HORDE", "AtakhanKill": "ATAKHAN",
}


def canonical_live_client_events(
        events: list[dict], name_to_participant: dict[str, int]) -> list[dict]:
    """Normalize Live Client Data's cumulative name-keyed events.

    Live Client Data (see live_client.py) reports events by display name,
    never by participant ID, and never reports a game/match ID at all --
    `name_to_participant` (built from the stored aggregate roster) is the
    only way to anchor these events to the participants everything else in
    this module is keyed by. A name that cannot be resolved is dropped
    rather than guessed, so it never silently attaches to the wrong player.
    """
    canonical: list[dict] = []
    for event in events or []:
        payload = event.get("payload") or {}
        event_time = event.get("event_time")
        etype = event.get("event_type") or payload.get("EventName")
        if event_time is None or etype is None:
            continue
        t_ms = float(event_time) * 1000.0
        entry: Optional[dict] = None
        if etype == "ChampionKill":
            killer = name_to_participant.get(payload.get("KillerName"))
            victim = name_to_participant.get(payload.get("VictimName"))
            assists = [
                name_to_participant[name]
                for name in (payload.get("Assisters") or [])
                if name in name_to_participant
            ]
            entry = {
                "kind": "champion_kill", "killer": killer, "victim": victim,
                "assists": assists, "position": None,
            }
        elif etype == "TurretKilled":
            entry = {
                "kind": "building_kill",
                "killer": name_to_participant.get(payload.get("KillerName")),
                "assists": [], "team_destroyed": None,
                "building_type": "TOWER_BUILDING", "tower_type": None,
                "lane": None, "position": None,
            }
        elif etype == "InhibKilled":
            entry = {
                "kind": "building_kill",
                "killer": name_to_participant.get(payload.get("KillerName")),
                "assists": [], "team_destroyed": None,
                "building_type": "INHIBITOR_BUILDING", "tower_type": None,
                "lane": None, "position": None,
            }
        elif etype in _LIVE_CLIENT_MONSTER_TYPES:
            entry = {
                "kind": "elite_monster_kill",
                "killer": name_to_participant.get(payload.get("KillerName")),
                "killer_team": None, "assists": [],
                "monster_type": _LIVE_CLIENT_MONSTER_TYPES[etype],
                "monster_sub_type": payload.get("DragonType"),
                "position": None,
            }
        if entry is not None:
            entry["t_ms"] = t_ms
            entry["phase"] = _phase_of(t_ms)
            entry["source"] = LIVE_CLIENT
            canonical.append(entry)
    canonical.sort(key=lambda e: e["t_ms"])
    return canonical


def map_live_client_names_to_participants(
        all_game_players: list[dict], participants: list[dict]) -> dict[str, int]:
    """Best-effort Live Client display-name -> participant_id map.

    Matches on champion identity plus a normalized Riot ID / summoner name,
    never a fuzzy guess -- an unmatched player is simply absent from the
    result (see `canonical_live_client_events`).
    """
    by_champion: dict[str, list[dict]] = {}
    for row in participants:
        by_champion.setdefault(row.get("champion_name"), []).append(row)
    mapping: dict[str, int] = {}
    for player in all_game_players or []:
        champion = player.get("championName")
        candidates = by_champion.get(champion) or []
        if len(candidates) != 1:
            continue
        names = {
            player.get("summonerName"),
            player.get("riotId"),
            player.get("riotIdGameName"),
        }
        game_name = player.get("riotIdGameName")
        tag_line = player.get("riotIdTagLine")
        if game_name and tag_line:
            names.add(f"{game_name}#{tag_line}")
        for display_name in names:
            if display_name:
                mapping[str(display_name)] = candidates[0]["participant_id"]
    return mapping


def canonical_live_client_snapshots(
        snapshots: list[dict],
        name_to_participant: dict[str, int]) -> dict[int, list[dict]]:
    """Extract only fields exposed comparably for all ten players."""
    per_participant: dict[int, list[dict]] = {}
    for stored in snapshots or []:
        payload = stored.get("payload") or {}
        game_time = payload.get("game_time")
        if not isinstance(game_time, (int, float)):
            continue
        for player in payload.get("players") or []:
            if not isinstance(player, dict):
                continue
            names = (
                player.get("summonerName"),
                player.get("riotId"),
                player.get("riotIdGameName"),
            )
            pid = next(
                (
                    name_to_participant[name]
                    for name in names
                    if name in name_to_participant
                ),
                None,
            )
            if pid is None:
                continue
            scores = player.get("scores") or {}
            per_participant.setdefault(pid, []).append({
                "t_ms": float(game_time) * 1000.0,
                "level": player.get("level"),
                "is_dead": bool(player.get("isDead")),
                "respawn_timer": player.get("respawnTimer"),
                "kills": scores.get("kills"),
                "deaths": scores.get("deaths"),
                "assists": scores.get("assists"),
                "cs": scores.get("creepScore"),
                "ward_score": scores.get("wardScore"),
                "item_count": len(player.get("items") or []),
            })
    for pid in per_participant:
        per_participant[pid].sort(key=lambda sample: sample["t_ms"])
    return per_participant


# ── per-participant feature families ────────────────────────────────────────

def _position_distance(a: object, b: object) -> Optional[float]:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return None
    if not all(isinstance(a.get(axis), (int, float)) for axis in ("x", "y")):
        return None
    if not all(isinstance(b.get(axis), (int, float)) for axis in ("x", "y")):
        return None
    return math.hypot(
        float(a["x"]) - float(b["x"]),
        float(a["y"]) - float(b["y"]),
    )


def _fight_influence(
        pid: int, team_of: dict, events: list[dict],
        first_blood_event: Optional[dict] = None) -> dict:
    team = team_of.get(pid)
    kill_events = [e for e in events if e["kind"] == "champion_kill"]
    kills = [e for e in kill_events if e.get("killer") == pid]
    deaths = [e for e in kill_events if e.get("victim") == pid]
    assists = [e for e in kill_events if pid in (e.get("assists") or [])]
    if first_blood_event is None:
        first_blood_event = kill_events[0] if kill_events else None
    first_blood = bool(
        first_blood_event
        and first_blood_event in kill_events
        and first_blood_event.get("killer") == pid
    )

    traded = 0
    untraded = 0
    unknown_trade_context = 0
    for death in deaths:
        window_lo, window_hi = death["t_ms"] - TRADE_WINDOW_MS, death["t_ms"] + TRADE_WINDOW_MS
        candidates = [
            other for other in kill_events
            if other is not death
            and window_lo <= other["t_ms"] <= window_hi
            and team_of.get(other.get("killer")) == team
        ]
        traded_back = any(
            (
                distance := _position_distance(
                    death.get("position"), other.get("position"),
                )
            ) is not None
            and distance <= CAUSAL_EVENT_DISTANCE
            for other in candidates
        )
        if traded_back:
            traded += 1
        elif not candidates:
            untraded += 1
        elif (
            isinstance(death.get("position"), dict)
            and all(isinstance(other.get("position"), dict) for other in candidates)
        ):
            untraded += 1
        else:
            unknown_trade_context += 1

    team_kills = [e for e in kill_events if team_of.get(e.get("killer")) == team]
    participation = len(kills) + len(assists)
    kill_participation_rate = (
        round(participation / len(team_kills), 4) if team_kills else None
    )

    return {
        "kill_events": len(kills),
        "death_events": len(deaths),
        "assist_events": len(assists),
        "first_blood": first_blood,
        "traded_deaths": traded,
        "untraded_deaths": untraded,
        "unknown_trade_context_deaths": unknown_trade_context,
        "event_kill_participation": kill_participation_rate,
    }


def _event_involves(event: dict, pid: int) -> bool:
    return (
        event.get("killer") == pid
        or event.get("victim") == pid
        or pid in (event.get("assists") or [])
    )


def _objective_participation(pid: int, events: list[dict]) -> dict:
    monster_events = [e for e in events if e["kind"] == "elite_monster_kill"]
    building_events = [e for e in events if e["kind"] == "building_kill"]
    plate_events = [e for e in events if e["kind"] == "turret_plate"]

    def secured(event: dict) -> bool:
        return event.get("killer") == pid

    def assisted(event: dict) -> bool:
        return pid in (event.get("assists") or [])

    grub_secures = sum(
        1 for e in monster_events
        if e.get("monster_type") in GRUB_MONSTER_TYPES and secured(e)
    )
    grub_assists = sum(
        1 for e in monster_events
        if e.get("monster_type") in GRUB_MONSTER_TYPES and assisted(e)
    )
    epic_secures = sum(
        1 for e in monster_events
        if e.get("monster_type") in EPIC_MONSTER_TYPES and secured(e)
    )
    epic_assists = sum(
        1 for e in monster_events
        if e.get("monster_type") in EPIC_MONSTER_TYPES and assisted(e)
    )
    turret_kills = sum(
        1 for e in building_events
        if e.get("building_type") == "TOWER_BUILDING" and secured(e)
    )
    turret_assists = sum(
        1 for e in building_events
        if e.get("building_type") == "TOWER_BUILDING" and assisted(e)
    )
    inhibitor_kills = sum(
        1 for e in building_events
        if e.get("building_type") == "INHIBITOR_BUILDING" and secured(e)
    )
    turret_plates = sum(1 for e in plate_events if secured(e))
    direct_monster_events = [
        event for event in monster_events
        if secured(event) or assisted(event)
    ]
    contest_supported = 0
    contest_unknown = 0
    for objective in direct_monster_events:
        nearby_fights = [
            fight for fight in events
            if fight.get("kind") == "champion_kill"
            and abs(fight["t_ms"] - objective["t_ms"]) <= OBJECTIVE_FIGHT_WINDOW_MS
            and _event_involves(fight, pid)
        ]
        if any(
            (
                distance := _position_distance(
                    objective.get("position"), fight.get("position"),
                )
            ) is not None
            and distance <= CAUSAL_EVENT_DISTANCE
            for fight in nearby_fights
        ):
            contest_supported += 1
        elif nearby_fights and (
            objective.get("position") is None
            or any(fight.get("position") is None for fight in nearby_fights)
        ):
            contest_unknown += 1

    return {
        "grub_secures": grub_secures,
        "grub_assists": grub_assists,
        "epic_monster_secures": epic_secures,
        "epic_monster_assists": epic_assists,
        "turret_kills": turret_kills,
        "turret_assists": turret_assists,
        "inhibitor_kills": inhibitor_kills,
        "turret_plates": turret_plates,
        "objective_fight_involvements": contest_supported,
        "unknown_objective_contest_context": contest_unknown,
        "direct_objective_contacts": len(direct_monster_events),
    }


def _count_isolated_samples(pid: int, frames_by_pid: dict, team_of: dict) -> Optional[int]:
    own = frames_by_pid.get(pid)
    if not own:
        return None
    team = team_of.get(pid)
    allies = [
        other_pid for other_pid, other_team in team_of.items()
        if other_team == team and other_pid != pid
    ]
    ally_frames = {a: {s["t_ms"]: s for s in frames_by_pid.get(a, [])} for a in allies}
    isolated = 0
    valid_samples = 0
    for sample in own:
        pos = sample.get("position")
        if not isinstance(pos, dict):
            continue
        nearest = math.inf
        for a in allies:
            ally_sample = ally_frames.get(a, {}).get(sample["t_ms"])
            if not ally_sample or not isinstance(ally_sample.get("position"), dict):
                continue
            dx = pos.get("x", 0) - ally_sample["position"].get("x", 0)
            dy = pos.get("y", 0) - ally_sample["position"].get("y", 0)
            nearest = min(nearest, math.hypot(dx, dy))
        if math.isinf(nearest):
            continue
        valid_samples += 1
        if nearest > SPLIT_ISOLATION_DISTANCE:
            isolated += 1
    return isolated if valid_samples else None


def _structure_pressure(
        pid: int, events: list[dict], frames_by_pid: dict, team_of: dict) -> dict:
    objective = _objective_participation(pid, events)
    lanes = sorted({
        e.get("lane") for e in events
        if e.get("kind") in ("building_kill", "turret_plate")
        and (e.get("killer") == pid or pid in (e.get("assists") or []))
        and e.get("lane")
    })
    return {
        "structure_secures": objective["turret_kills"] + objective["inhibitor_kills"],
        "structure_assists": objective["turret_assists"],
        "turret_plates": objective["turret_plates"],
        "lanes_pressured": lanes,
        "isolated_frame_samples": _count_isolated_samples(pid, frames_by_pid, team_of),
    }


def _enablement_suppression(
        pid: int, team_of: dict, events: list[dict], enemy_pressure: dict) -> dict:
    team = team_of.get(pid)
    kill_events = [e for e in events if e["kind"] == "champion_kill"]
    ally_enablement = sum(
        1 for e in kill_events
        if pid in (e.get("assists") or []) and e.get("killer") != pid
        and team_of.get(e.get("killer")) == team
    )
    suppression_events = 0
    suppression_weight = 0.0
    for e in kill_events:
        involved = e.get("killer") == pid or pid in (e.get("assists") or [])
        if not involved:
            continue
        victim = e.get("victim")
        if victim is None or team_of.get(victim) == team:
            continue
        suppression_events += 1
        suppression_weight += enemy_pressure.get(victim, 0)
    return {
        "ally_enablement_assists": ally_enablement,
        "suppression_events": suppression_events,
        "suppression_weight": round(suppression_weight, 4),
    }


def _event_benefits_team(event: dict, team: int, team_of: dict) -> bool:
    killer = event.get("killer")
    if killer is not None and team_of.get(killer) == team:
        return True
    if event.get("killer_team") is not None:
        return event.get("killer_team") == team
    return any(
        team_of.get(assistant) == team
        for assistant in event.get("assists") or []
    )


def _latest_frame_position(
        pid: int, t_ms: float,
        frames_by_pid: dict[int, list[dict]]) -> Optional[dict]:
    candidates = [
        sample for sample in frames_by_pid.get(pid, [])
        if sample["t_ms"] <= t_ms
        and t_ms - sample["t_ms"] <= FRAME_POSITION_MAX_AGE_MS
        and isinstance(sample.get("position"), dict)
    ]
    return candidates[-1]["position"] if candidates else None


def _vision_influence(
        pid: int, team_of: dict, events: list[dict], has_ward_events: bool,
        frames_by_pid: dict[int, list[dict]]) -> dict:
    if not has_ward_events:
        return {
            "available": False,
            "reason": (
                "This evidence source does not carry ward/item events "
                "(verified for the LCU historical timeline); raw vision "
                "stats remain in `raw` but must not be treated as influence."
            ),
        }
    placed = [e for e in events if e["kind"] == "ward_placed" and e.get("actor") == pid]
    killed = [e for e in events if e["kind"] == "ward_kill" and e.get("actor") == pid]
    followup_candidates = [
        e for e in events if e["kind"] in ("champion_kill", "elite_monster_kill", "building_kill")
    ]
    team = team_of.get(pid)

    def has_followup(ward: dict) -> bool:
        ward_position = ward.get("position") or _latest_frame_position(
            pid, ward["t_ms"], frames_by_pid,
        )
        return any(
            ward["t_ms"] <= e["t_ms"] <= ward["t_ms"] + VISION_ACTIONABLE_WINDOW_MS
            and _event_benefits_team(e, team, team_of)
            and (
                (
                    distance := _position_distance(
                        ward_position, e.get("position"),
                    )
                ) is not None
                and distance <= CAUSAL_EVENT_DISTANCE
            )
            for e in followup_candidates
        )

    actionable_wards = sum(1 for w in placed if has_followup(w))
    actionable_dewards = sum(1 for w in killed if has_followup(w))
    total = len(placed) + len(killed)
    rate = (
        round((actionable_wards + actionable_dewards) / total, 4) if total else None
    )
    return {
        "available": True,
        "wards_placed_events": len(placed),
        "wards_killed_events": len(killed),
        "actionable_wards": actionable_wards,
        "actionable_dewards": actionable_dewards,
        "vision_actionable_rate": rate,
    }


def _live_state_summary(pid: int, live_series: dict[int, list[dict]]) -> dict:
    samples = live_series.get(pid) or []
    if not samples:
        return {
            "available": False,
            "reason": "no ten-player-comparable Live Client snapshots",
        }
    final = samples[-1]
    dead_samples = sum(1 for sample in samples if sample.get("is_dead"))
    respawn_timers = [
        float(sample["respawn_timer"]) for sample in samples
        if isinstance(sample.get("respawn_timer"), (int, float))
    ]
    return {
        "available": True,
        "observed_samples": len(samples),
        "final_level": final.get("level"),
        "final_kills": final.get("kills"),
        "final_deaths": final.get("deaths"),
        "final_assists": final.get("assists"),
        "final_cs": final.get("cs"),
        "final_ward_score": final.get("ward_score"),
        "final_item_count": final.get("item_count"),
        "dead_sample_rate": round(dead_samples / len(samples), 4),
        "max_observed_respawn_timer": max(respawn_timers) if respawn_timers else None,
    }


def _death_tempo(pid: int, events: list[dict]) -> dict:
    deaths = sorted(
        (e for e in events if e["kind"] == "champion_kill" and e.get("victim") == pid),
        key=lambda e: e["t_ms"],
    )
    intervals = [
        round(b["t_ms"] - a["t_ms"], 1) for a, b in zip(deaths, deaths[1:])
    ]
    rapid_pairs = sum(1 for gap in intervals if gap <= RAPID_DEATH_WINDOW_MS)
    by_phase = {"early": 0, "mid": 0, "late": 0}
    for death in deaths:
        by_phase[death["phase"]] += 1
    return {
        "death_count": len(deaths),
        "deaths_by_phase": by_phase,
        "rapid_death_pairs": rapid_pairs,
        "min_death_interval_ms": min(intervals) if intervals else None,
    }


def _find_lane_opponent(pid: int, role_of: dict, team_of: dict) -> Optional[int]:
    role = role_of.get(pid)
    if role not in _LANE_ROLES:
        return None
    team = team_of.get(pid)
    for other_pid, other_role in role_of.items():
        if other_pid != pid and other_role == role and team_of.get(other_pid) != team:
            return other_pid
    return None


def _resource_conversion(
        pid: int, role_of: dict, team_of: dict, frames_by_pid: dict,
        events: list[dict]) -> dict:
    own = frames_by_pid.get(pid)
    if not own:
        return {"available": False, "reason": "no per-minute frames for this participant"}
    opponent = _find_lane_opponent(pid, role_of, team_of)
    opponent_frames = frames_by_pid.get(opponent) if opponent is not None else None
    if not opponent_frames:
        return {"available": False, "reason": "no comparable lane-opponent frames"}
    opponent_by_t = {sample["t_ms"]: sample for sample in opponent_frames}
    participation = [
        e for e in events
        if e["kind"] in ("champion_kill", "elite_monster_kill", "building_kill", "turret_plate")
    ]
    lead_windows = 0
    converted = 0
    for sample in own:
        opp_sample = opponent_by_t.get(sample["t_ms"])
        if not opp_sample or sample.get("gold") is None or opp_sample.get("gold") is None:
            continue
        lead = sample["gold"] - opp_sample["gold"]
        if lead <= GOLD_LEAD_THRESHOLD:
            continue
        lead_windows += 1
        window_hi = sample["t_ms"] + CONVERSION_WINDOW_MS
        converted_here = any(
            sample["t_ms"] < e["t_ms"] <= window_hi
            and (e.get("killer") == pid or pid in (e.get("assists") or []))
            for e in participation
        )
        if converted_here:
            converted += 1
    return {
        "available": True,
        "lane_opponent": opponent,
        "lead_windows": lead_windows,
        "converted_lead_windows": converted,
        "conversion_rate": round(converted / lead_windows, 4) if lead_windows else None,
    }


def _phase_breakdown(pid: int, team_of: dict, events: list[dict]) -> dict:
    """Bucket fight/objective evidence into non-overlapping phase windows.

    Each phase is computed from ONLY the events whose timestamp already
    falls inside that phase's [start, end) window -- a later phase's events
    are never visible while computing an earlier phase's numbers.
    """
    breakdown = {}
    kill_events = [
        event for event in events if event.get("kind") == "champion_kill"
    ]
    first_blood_event = kill_events[0] if kill_events else None
    for name, start, end in PHASE_BOUNDARIES_MS:
        window_events = [e for e in events if start <= e["t_ms"] < end]
        breakdown[name] = {
            "fight": _fight_influence(
                pid, team_of, window_events,
                first_blood_event=first_blood_event,
            ),
            "objective": _objective_participation(pid, window_events),
        }
    return breakdown


def _signed_event_evidence(events: list[dict]) -> list[dict]:
    evidence = []

    def add(event: dict, pid: Optional[int], sign: int, metric: str) -> None:
        if pid is None:
            return
        evidence.append({
            "kind": "signed_event",
            "participant_id": int(pid),
            "t_ms": event["t_ms"],
            "phase": event["phase"],
            "source": event["source"],
            "sign": sign,
            "metric": metric,
        })

    for event in events:
        if event["kind"] == "champion_kill":
            add(event, event.get("killer"), 1, "champion_kill")
            add(event, event.get("victim"), -1, "death")
            for assistant in event.get("assists") or []:
                add(event, assistant, 1, "champion_kill_assist")
        elif event["kind"] == "elite_monster_kill":
            add(event, event.get("killer"), 1, "objective_secure")
            for assistant in event.get("assists") or []:
                add(event, assistant, 1, "objective_assist")
        elif event["kind"] in ("building_kill", "turret_plate"):
            add(event, event.get("killer"), 1, "structure_secure")
            for assistant in event.get("assists") or []:
                add(event, assistant, 1, "structure_assist")
    evidence.sort(
        key=lambda item: (
            item["t_ms"], item["participant_id"], item["metric"], item["sign"],
        ),
    )
    return evidence


def _baseline_inputs(role: str, champion_name: str, patch: Optional[str]) -> dict:
    """Role/champion identity inputs a future model can shrink toward.

    This deliberately does not compute a shrunk estimate or a sample size
    itself -- that requires the historical corpus, which is out of scope
    for per-game feature extraction (see the score-v2-features todo: no
    model training here). It only names the grouping keys so training code
    has a stable place to look.
    """
    return {
        "role": role,
        "champion": champion_name,
        "patch": patch,
        "shrinkage_grouping_keys": ["role", "champion"],
    }


# ── top-level extraction ────────────────────────────────────────────────────

_OUTCOME_KEYS = ("win", "local_win")


def compute_feature_set(
        participants: list[dict], duration_seconds: float,
        capabilities: EvidenceCapabilities, evidence_source: str,
        timeline: Optional[dict] = None,
        live_events: Optional[list[dict]] = None,
        live_snapshots: Optional[dict[int, list[dict]]] = None,
        patch: Optional[str] = None) -> tuple[dict, list[dict]]:
    """Extract source-aware Score v2 features for one match.

    `participants` are raw participant rows (as from
    `HistoryStore.get_participants`); any `win`/`local_win` keys are
    stripped before use so no code path here can read a result flag.
    `timeline` is the raw provider payload's `timeline` dict for
    `match_v5`/`lcu_timeline` sources (top-level `frames`, or `info.frames`
    for Match-V5). `live_events` is a pre-normalized
    `canonical_live_client_events` list for the `live_client` source.

    Returns `(features, evidence)` -- pass both straight to
    `HistoryStore.save_feature_set(game_id, feature_version, evidence_source,
    features, evidence)`.
    """
    if len(participants) != 10:
        raise ValueError("Score v2 feature extraction requires exactly 10 participants")
    if evidence_source not in SOURCE_PRIORITY:
        raise ValueError(f"Unknown evidence source {evidence_source!r}")

    team_of: dict[int, int] = {}
    role_of: dict[int, str] = {}
    champion_of: dict[int, str] = {}
    raw_rows: dict[int, dict] = {}
    for row in participants:
        pid = row["participant_id"]
        team_of[pid] = row["team_id"]
        role_of[pid] = row.get("role", "unknown")
        champion_of[pid] = row.get("champion_name", "unknown")
        raw_rows[pid] = {k: v for k, v in row.items() if k not in _OUTCOME_KEYS}

    events: list[dict] = []
    frames_by_pid: dict[int, list[dict]] = {}
    live_series = live_snapshots or {}
    has_ward_events = False
    if evidence_source in (MATCH_V5, LCU_TIMELINE) and timeline:
        frames = timeline.get("frames")
        if frames is None:
            frames = (timeline.get("info") or {}).get("frames")
        events = canonical_timeline_events(frames or [], evidence_source)
        frames_by_pid = canonical_timeline_frames(frames or [])
        has_ward_events = any(e["kind"] in ("ward_placed", "ward_kill") for e in events)
    elif evidence_source == LIVE_CLIENT and live_events:
        events = list(live_events)
        has_ward_events = False

    has_event_evidence = bool(events)
    has_observable_evidence = (
        has_event_evidence or bool(frames_by_pid) or bool(live_series)
    )

    kill_events_all = [e for e in events if e["kind"] == "champion_kill"]
    enemy_pressure: dict[int, int] = {}
    for pid in team_of:
        enemy_pressure[pid] = sum(
            1 for e in kill_events_all
            if e.get("killer") == pid or pid in (e.get("assists") or [])
        )

    abstain = float(duration_seconds) < SHORT_GAME_ABSTAIN_SECONDS

    participant_features = {}
    for pid in sorted(team_of):
        row = raw_rows[pid]
        raw_block = {
            "kills": row.get("kills"), "deaths": row.get("deaths"),
            "assists": row.get("assists"), "gold_earned": row.get("gold_earned"),
            "cs": row.get("cs"), "vision_score": row.get("vision_score"),
            "wards_placed": row.get("wards_placed"),
            "wards_killed": row.get("wards_killed"),
            "damage_to_champions": row.get("damage_to_champions"),
            "damage_to_objectives": row.get("damage_to_objectives"),
            "damage_to_turrets": row.get("damage_to_turrets"),
        }
        if not has_observable_evidence:
            participant_features[str(pid)] = {
                "team_id": team_of[pid],
                "raw": raw_block,
                "fight_influence": None,
                "resource_conversion": {
                    "available": False,
                    "reason": "no per-minute frames for this evidence tier",
                },
                "objective_participation": None,
                "structure_pressure": None,
                "enablement_suppression": None,
                "vision_influence": {
                    "available": False,
                    "reason": "no event-level timeline for this evidence tier",
                },
                "death_tempo": None,
                "phase_breakdown": None,
                "live_state": _live_state_summary(pid, live_series),
                "baseline": _baseline_inputs(
                    role_of[pid], champion_of[pid], patch,
                ),
            }
            continue

        participant_features[str(pid)] = {
            "team_id": team_of[pid],
            "raw": raw_block,
            "fight_influence": (
                _fight_influence(pid, team_of, events)
                if has_event_evidence else None
            ),
            "resource_conversion": _resource_conversion(
                pid, role_of, team_of, frames_by_pid, events,
            ),
            "objective_participation": (
                _objective_participation(pid, events)
                if has_event_evidence else None
            ),
            "structure_pressure": (
                _structure_pressure(pid, events, frames_by_pid, team_of)
                if has_event_evidence else None
            ),
            "enablement_suppression": (
                _enablement_suppression(pid, team_of, events, enemy_pressure)
                if has_event_evidence else None
            ),
            "vision_influence": (
                _vision_influence(
                    pid, team_of, events, has_ward_events, frames_by_pid,
                )
                if has_event_evidence else {
                    "available": False,
                    "reason": "no event-level timeline for this evidence tier",
                }
            ),
            "death_tempo": (
                _death_tempo(pid, events) if has_event_evidence else None
            ),
            "phase_breakdown": (
                _phase_breakdown(pid, team_of, events)
                if has_event_evidence else None
            ),
            "live_state": _live_state_summary(pid, live_series),
            "baseline": _baseline_inputs(
                role_of[pid], champion_of[pid], patch,
            ),
        }

    features = {
        "feature_version": FEATURE_VERSION,
        "evidence_source": evidence_source,
        "capabilities": capabilities.as_dict(),
        "evidence_quality": capabilities.quality_dict(),
        "chosen_source_completeness": capabilities.source_completeness(
            evidence_source,
        ),
        "duration_seconds": duration_seconds,
        "abstain": abstain,
        "abstain_reason": "short_game" if abstain else None,
        "participants": participant_features,
    }
    evidence = [
        {
            "kind": "capability_snapshot",
            "capabilities": capabilities.as_dict(),
            "chosen_source": evidence_source,
        },
        {"kind": "event_count", "count": len(events)},
        {"kind": "frame_participant_count", "count": len(frames_by_pid)},
        {"kind": "live_snapshot_participant_count", "count": len(live_series)},
    ]
    evidence.extend(_signed_event_evidence(events))
    return features, evidence


def extract_game_features(
        store, game_id: int, feature_version: str = FEATURE_VERSION) -> dict:
    """Detect the best evidence tier for `game_id`, extract, and persist.

    This is the only HistoryStore integration point in this module --
    everything else (`compute_feature_set` and the canonical_* helpers) is
    pure and testable against fixtures with no store at all.
    """
    match_row = store.get_match(game_id)
    if match_row is None:
        raise ValueError(f"Unknown game ID {game_id}")
    participants = store.get_participants(game_id)
    capabilities = detect_capabilities(store, game_id)
    source = capabilities.best_source()

    timeline = None
    live_events = None
    live_snapshots = None
    if source in (MATCH_V5, LCU_TIMELINE):
        stored = store.get_timeline_payload(game_id, source)
        timeline = ((stored or {}).get("payload") or {}).get("timeline")
    elif source == LIVE_CLIENT:
        session = _best_live_session(store, game_id)
        if session is not None:
            raw_events = store.get_live_capture_events(session["session_id"])
            snapshots = store.get_live_capture_snapshots(session["session_id"])
            all_game_players = []
            if snapshots:
                all_game_players = (snapshots[-1].get("payload") or {}).get("players") or []
            name_to_pid = map_live_client_names_to_participants(
                all_game_players, participants,
            )
            live_events = canonical_live_client_events(raw_events, name_to_pid)
            live_snapshots = canonical_live_client_snapshots(
                snapshots, name_to_pid,
            )

    features, evidence = compute_feature_set(
        participants, match_row["duration"], capabilities, source,
        timeline=timeline, live_events=live_events,
        live_snapshots=live_snapshots, patch=match_row.get("patch"),
    )
    store.save_feature_set(game_id, feature_version, source, features, evidence)
    return features
