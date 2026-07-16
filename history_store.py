"""Local SQLite persistence for RuneSync post-game analytics."""

import datetime
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


SCHEMA_VERSION = 1


def default_history_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    directory = base / "RuneSync"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        directory = Path(tempfile.gettempdir()) / "RuneSync"
        directory.mkdir(parents=True, exist_ok=True)
    return directory / "history.db"


class HistoryStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_history_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS matches (
                    game_id INTEGER PRIMARY KEY,
                    queue_id INTEGER NOT NULL,
                    map_id INTEGER NOT NULL,
                    game_mode TEXT NOT NULL,
                    game_creation INTEGER NOT NULL,
                    game_creation_date TEXT NOT NULL,
                    duration INTEGER NOT NULL,
                    patch TEXT NOT NULL,
                    local_participant_id INTEGER NOT NULL,
                    local_win INTEGER NOT NULL,
                    local_champion_id INTEGER NOT NULL,
                    local_champion_name TEXT NOT NULL,
                    local_role TEXT NOT NULL,
                    score_model_version INTEGER NOT NULL,
                    imported_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS participants (
                    game_id INTEGER NOT NULL REFERENCES matches(game_id) ON DELETE CASCADE,
                    participant_id INTEGER NOT NULL,
                    puuid TEXT NOT NULL,
                    summoner_name TEXT NOT NULL,
                    champion_id INTEGER NOT NULL,
                    champion_name TEXT NOT NULL,
                    team_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    win INTEGER NOT NULL,
                    kills INTEGER NOT NULL,
                    deaths INTEGER NOT NULL,
                    assists INTEGER NOT NULL,
                    gold_earned INTEGER NOT NULL,
                    cs INTEGER NOT NULL,
                    champion_level INTEGER NOT NULL,
                    damage_to_champions INTEGER NOT NULL,
                    damage_to_objectives INTEGER NOT NULL,
                    damage_to_turrets INTEGER NOT NULL,
                    damage_taken INTEGER NOT NULL,
                    damage_mitigated INTEGER NOT NULL,
                    healing INTEGER NOT NULL,
                    vision_score INTEGER NOT NULL,
                    wards_placed INTEGER NOT NULL,
                    wards_killed INTEGER NOT NULL,
                    items_json TEXT NOT NULL,
                    PRIMARY KEY (game_id, participant_id)
                );

                CREATE TABLE IF NOT EXISTS scores (
                    game_id INTEGER NOT NULL,
                    participant_id INTEGER NOT NULL,
                    model_version INTEGER NOT NULL,
                    total_score REAL NOT NULL,
                    match_rank INTEGER NOT NULL,
                    components_json TEXT NOT NULL,
                    observations_json TEXT NOT NULL,
                    PRIMARY KEY (game_id, participant_id),
                    FOREIGN KEY (game_id, participant_id)
                        REFERENCES participants(game_id, participant_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_matches_creation
                    ON matches(game_creation DESC);
                CREATE INDEX IF NOT EXISTS idx_matches_champion
                    ON matches(local_champion_name);
                CREATE INDEX IF NOT EXISTS idx_matches_role
                    ON matches(local_role);
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def has_game(self, game_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM matches WHERE game_id = ?", (game_id,),
            ).fetchone()
        return row is not None

    def known_game_ids(self) -> set[int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT game_id FROM matches").fetchall()
        return {int(row["game_id"]) for row in rows}

    def get_meta(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (key, str(value)),
            )

    def save_report(self, report: dict) -> None:
        match = report["match"]
        participants = report["participants"]
        scores = report["scores"]
        imported_at = match.get("imported_at") or datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO matches (
                    game_id, queue_id, map_id, game_mode, game_creation,
                    game_creation_date, duration, patch, local_participant_id,
                    local_win, local_champion_id, local_champion_name, local_role,
                    score_model_version, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match["game_id"], match["queue_id"], match["map_id"],
                    match["game_mode"], match["game_creation"],
                    match["game_creation_date"], match["duration"], match["patch"],
                    match["local_participant_id"], int(match["local_win"]),
                    match["local_champion_id"], match["local_champion_name"],
                    match["local_role"], match["score_model_version"], imported_at,
                ),
            )
            conn.execute("DELETE FROM participants WHERE game_id = ?", (match["game_id"],))
            for player in participants:
                conn.execute(
                    """
                    INSERT INTO participants (
                        game_id, participant_id, puuid, summoner_name, champion_id,
                        champion_name, team_id, role, win, kills, deaths, assists,
                        gold_earned, cs, champion_level, damage_to_champions,
                        damage_to_objectives, damage_to_turrets, damage_taken,
                        damage_mitigated, healing, vision_score, wards_placed,
                        wards_killed, items_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        match["game_id"], player["participant_id"], player["puuid"],
                        player["summoner_name"], player["champion_id"],
                        player["champion_name"], player["team_id"], player["role"],
                        int(player["win"]), player["kills"], player["deaths"],
                        player["assists"], player["gold_earned"], player["cs"],
                        player["champion_level"], player["damage_to_champions"],
                        player["damage_to_objectives"], player["damage_to_turrets"],
                        player["damage_taken"], player["damage_mitigated"],
                        player["healing"], player["vision_score"],
                        player["wards_placed"], player["wards_killed"],
                        json.dumps(player["items"], separators=(",", ":")),
                    ),
                )
            for score in scores:
                conn.execute(
                    """
                    INSERT INTO scores (
                        game_id, participant_id, model_version, total_score,
                        match_rank, components_json, observations_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        match["game_id"], score["participant_id"],
                        score["model_version"], score["total_score"],
                        score["match_rank"],
                        json.dumps(score["components"], separators=(",", ":")),
                        json.dumps(score["observations"], separators=(",", ":")),
                    ),
                )

    def get_summary(self) -> dict:
        with self._connect() as conn:
            totals = conn.execute(
                "SELECT COUNT(*) AS games, COALESCE(SUM(local_win), 0) AS wins FROM matches"
            ).fetchone()
            recent = conn.execute(
                """
                SELECT COUNT(*) AS games, COALESCE(SUM(local_win), 0) AS wins
                FROM (
                    SELECT local_win FROM matches
                    ORDER BY game_creation DESC LIMIT 20
                )
                """
            ).fetchone()
            champions = conn.execute(
                """
                SELECT local_champion_name AS name, COUNT(*) AS games,
                       SUM(local_win) AS wins
                FROM matches
                GROUP BY local_champion_name
                ORDER BY games DESC, name ASC
                """
            ).fetchall()
            roles = conn.execute(
                """
                SELECT local_role AS name, COUNT(*) AS games, SUM(local_win) AS wins
                FROM matches
                GROUP BY local_role
                ORDER BY games DESC, name ASC
                """
            ).fetchall()
            performance = conn.execute(
                """
                SELECT ROUND(AVG(s.total_score), 1) AS average_score,
                       MIN(s.match_rank) AS best_rank,
                       ROUND(AVG(s.match_rank), 1) AS average_rank
                FROM matches m
                JOIN scores s
                  ON s.game_id = m.game_id
                 AND s.participant_id = m.local_participant_id
                """
            ).fetchone()

        def group_rows(rows):
            return [
                {
                    "name": row["name"],
                    "games": row["games"],
                    "wins": row["wins"],
                    "win_rate": round(row["wins"] * 100 / row["games"], 1),
                }
                for row in rows
            ]

        return {
            "overall": self._rate_row(totals),
            "recent20": self._rate_row(recent),
            "champions": group_rows(champions),
            "roles": group_rows(roles),
            "performance": {
                "average_score": performance["average_score"],
                "best_rank": performance["best_rank"],
                "average_rank": performance["average_rank"],
            },
        }

    @staticmethod
    def _rate_row(row: sqlite3.Row) -> dict:
        games = int(row["games"])
        wins = int(row["wins"])
        return {
            "games": games,
            "wins": wins,
            "win_rate": round(wins * 100 / games, 1) if games else None,
        }

    def list_history(self, offset: int = 0, limit: int = 25) -> list[dict]:
        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.*, p.kills, p.deaths, p.assists, p.cs, p.gold_earned,
                       s.total_score, s.match_rank
                FROM matches m
                JOIN participants p
                  ON p.game_id = m.game_id
                 AND p.participant_id = m.local_participant_id
                JOIN scores s
                  ON s.game_id = m.game_id
                 AND s.participant_id = m.local_participant_id
                ORDER BY m.game_creation DESC
                LIMIT ? OFFSET ?
                """,
                (safe_limit, safe_offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_report(self, game_id: int) -> Optional[dict]:
        with self._connect() as conn:
            match = conn.execute(
                "SELECT * FROM matches WHERE game_id = ?", (game_id,),
            ).fetchone()
            if not match:
                return None
            players = conn.execute(
                """
                SELECT p.*, s.model_version, s.total_score, s.match_rank,
                       s.components_json, s.observations_json
                FROM participants p
                JOIN scores s
                  ON s.game_id = p.game_id
                 AND s.participant_id = p.participant_id
                WHERE p.game_id = ?
                ORDER BY p.team_id ASC,
                         CASE p.role
                             WHEN 'top' THEN 1
                             WHEN 'jungle' THEN 2
                             WHEN 'mid' THEN 3
                             WHEN 'bot' THEN 4
                             WHEN 'bottom' THEN 4
                             WHEN 'support' THEN 5
                             ELSE 6
                         END ASC,
                         p.participant_id ASC
                """,
                (game_id,),
            ).fetchall()

        participants = []
        for row in players:
            item = dict(row)
            item["items"] = json.loads(item.pop("items_json"))
            item["components"] = json.loads(item.pop("components_json"))
            item["observations"] = json.loads(item.pop("observations_json"))
            participants.append(item)
        return {"match": dict(match), "participants": participants}
