"""Local SQLite persistence for RuneSync post-game analytics."""

import datetime
import hashlib
import json
import os
import sqlite3
import tempfile
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


SCHEMA_VERSION = 4

SCORE_EVIDENCE_PRIORITY = {
    "aggregate_legacy": -1,
    "aggregate": 0,
    "live_client": 1,
    "lcu_timeline": 2,
    "match_v5": 3,
}


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
        self._backup_before_migration()
        self._migrate()

    def _backup_before_migration(self) -> None:
        if not self.path.exists() or not self.path.stat().st_size:
            return
        previous_version = 0
        try:
            source = sqlite3.connect(self.path, timeout=10)
            try:
                row = source.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                previous_version = int(row[0]) if row else 0
                if previous_version >= SCHEMA_VERSION:
                    return
                backup = self.path.with_name(
                    f"{self.path.name}.schema-v{previous_version}.bak"
                )
                if not backup.exists():
                    destination = sqlite3.connect(backup)
                    try:
                        source.backup(destination)
                    finally:
                        destination.close()
            finally:
                source.close()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return

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
                    imported_at TEXT NOT NULL,
                    active_score_run_id INTEGER
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
                CREATE INDEX IF NOT EXISTS idx_matches_role_creation
                    ON matches(local_role, game_creation DESC, game_id DESC);
                """
            )
            match_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(matches)")
            }
            if "active_score_run_id" not in match_columns:
                conn.execute(
                    "ALTER TABLE matches ADD COLUMN active_score_run_id INTEGER"
                )
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS score_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id INTEGER NOT NULL REFERENCES matches(game_id)
                        ON DELETE CASCADE,
                    model_version INTEGER NOT NULL,
                    feature_version TEXT NOT NULL,
                    evidence_source TEXT NOT NULL,
                    calibration_version TEXT NOT NULL,
                    model_artifact_hash TEXT NOT NULL,
                    artifact_model_version TEXT NOT NULL DEFAULT '',
                    model_family TEXT NOT NULL DEFAULT 'legacy',
                    input_hash TEXT NOT NULL,
                    confidence_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (
                        game_id, model_version, feature_version,
                        evidence_source, calibration_version,
                        model_artifact_hash, input_hash
                    )
                );

                CREATE TABLE IF NOT EXISTS score_results (
                    run_id INTEGER NOT NULL REFERENCES score_runs(id)
                        ON DELETE CASCADE,
                    game_id INTEGER NOT NULL,
                    participant_id INTEGER NOT NULL,
                    total_score REAL NOT NULL,
                    match_rank INTEGER NOT NULL,
                    components_json TEXT NOT NULL,
                    observations_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    score_low REAL,
                    score_high REAL,
                    participant_confidence REAL,
                    rank_confidence REAL,
                    abstain INTEGER NOT NULL DEFAULT 0,
                    abstain_reasons_json TEXT NOT NULL DEFAULT '[]',
                    coaching_json TEXT NOT NULL DEFAULT '{}',
                    coaching_eligible INTEGER NOT NULL,
                    PRIMARY KEY (run_id, participant_id),
                    FOREIGN KEY (game_id, participant_id)
                        REFERENCES participants(game_id, participant_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS timeline_payloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id INTEGER NOT NULL REFERENCES matches(game_id)
                        ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    completeness REAL NOT NULL,
                    content_hash TEXT NOT NULL,
                    payload_zlib BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (game_id, source, content_hash)
                );

                CREATE TABLE IF NOT EXISTS timeline_fetch_attempts (
                    game_id INTEGER NOT NULL REFERENCES matches(game_id)
                        ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    last_attempted_at TEXT NOT NULL,
                    next_retry_at TEXT NOT NULL,
                    last_error_kind TEXT NOT NULL,
                    PRIMARY KEY (game_id, source)
                );

                CREATE TABLE IF NOT EXISTS live_capture_sessions (
                    session_id TEXT PRIMARY KEY,
                    game_id INTEGER,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL,
                    completeness REAL NOT NULL,
                    last_event_id INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_capture_events (
                    session_id TEXT NOT NULL REFERENCES live_capture_sessions(session_id)
                        ON DELETE CASCADE,
                    event_id INTEGER NOT NULL,
                    event_time REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, event_id)
                );

                CREATE TABLE IF NOT EXISTS live_capture_snapshots (
                    session_id TEXT NOT NULL REFERENCES live_capture_sessions(session_id)
                        ON DELETE CASCADE,
                    snapshot_time REAL NOT NULL,
                    content_hash TEXT NOT NULL,
                    payload_zlib BLOB NOT NULL,
                    PRIMARY KEY (session_id, snapshot_time)
                );

                CREATE TABLE IF NOT EXISTS feature_sets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id INTEGER NOT NULL REFERENCES matches(game_id)
                        ON DELETE CASCADE,
                    feature_version TEXT NOT NULL,
                    evidence_source TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (
                        game_id, feature_version, evidence_source, input_hash
                    )
                );

                CREATE INDEX IF NOT EXISTS idx_score_runs_game
                    ON score_runs(game_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_score_results_game
                    ON score_results(game_id, participant_id);
                CREATE INDEX IF NOT EXISTS idx_timeline_payloads_game
                    ON timeline_payloads(game_id, source, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_feature_sets_game
                    ON feature_sets(game_id, feature_version, evidence_source);
                CREATE INDEX IF NOT EXISTS idx_feature_sets_lookup
                    ON feature_sets(
                        game_id, feature_version, evidence_source,
                        created_at DESC, id DESC
                    );

                CREATE TRIGGER IF NOT EXISTS prevent_active_score_run_delete
                BEFORE DELETE ON score_runs
                WHEN EXISTS (
                    SELECT 1 FROM matches
                    WHERE active_score_run_id = OLD.id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'cannot delete active score run');
                END;
                """
            )
            score_run_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(score_runs)")
            }
            if "artifact_model_version" not in score_run_columns:
                conn.execute(
                    "ALTER TABLE score_runs ADD COLUMN "
                    "artifact_model_version TEXT NOT NULL DEFAULT ''"
                )
            if "model_family" not in score_run_columns:
                conn.execute(
                    "ALTER TABLE score_runs ADD COLUMN "
                    "model_family TEXT NOT NULL DEFAULT 'legacy'"
                )
            score_result_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(score_results)")
            }
            if "participant_confidence" not in score_result_columns:
                conn.execute(
                    "ALTER TABLE score_results ADD COLUMN participant_confidence REAL"
                )
            if "abstain" not in score_result_columns:
                conn.execute(
                    "ALTER TABLE score_results ADD COLUMN "
                    "abstain INTEGER NOT NULL DEFAULT 0"
                )
            if "abstain_reasons_json" not in score_result_columns:
                conn.execute(
                    "ALTER TABLE score_results ADD COLUMN "
                    "abstain_reasons_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "coaching_json" not in score_result_columns:
                conn.execute(
                    "ALTER TABLE score_results ADD COLUMN "
                    "coaching_json TEXT NOT NULL DEFAULT '{}'"
                )
            self._migrate_legacy_scores(conn)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _json(value) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _hash(cls, value) -> str:
        return hashlib.sha256(cls._json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _hash_bytes(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()

    def _migrate_legacy_scores(self, conn: sqlite3.Connection) -> None:
        matches = conn.execute(
            "SELECT game_id, active_score_run_id FROM matches"
        ).fetchall()
        for match in matches:
            if match["active_score_run_id"]:
                continue
            rows = conn.execute(
                """
                SELECT participant_id, model_version, total_score, match_rank,
                       components_json, observations_json
                FROM scores WHERE game_id = ?
                ORDER BY participant_id
                """,
                (match["game_id"],),
            ).fetchall()
            if not rows:
                continue
            payload = [dict(row) for row in rows]
            input_hash = self._hash(payload)
            model_version = int(rows[0]["model_version"])
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO score_runs (
                    game_id, model_version, feature_version, evidence_source,
                    calibration_version, model_artifact_hash, input_hash,
                    confidence_json, status, created_at
                ) VALUES (?, ?, 'v1', 'aggregate_legacy', 'v1',
                          'legacy-inline', ?, ?, 'complete', ?)
                """,
                (
                    match["game_id"], model_version, input_hash,
                    self._json({
                        "evidence_quality": "legacy",
                        "coaching_eligible": False,
                    }),
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ),
            )
            run_id = cursor.lastrowid
            if not run_id:
                run_id = conn.execute(
                    """
                    SELECT id FROM score_runs
                    WHERE game_id = ? AND model_version = ?
                      AND feature_version = 'v1'
                      AND evidence_source = 'aggregate_legacy'
                      AND calibration_version = 'v1'
                      AND model_artifact_hash = 'legacy-inline'
                      AND input_hash = ?
                    """,
                    (match["game_id"], model_version, input_hash),
                ).fetchone()["id"]
            for row in rows:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO score_results (
                        run_id, game_id, participant_id, total_score, match_rank,
                        components_json, observations_json, evidence_json,
                        score_low, score_high, rank_confidence,
                        coaching_eligible
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', NULL, NULL, NULL, 0)
                    """,
                    (
                        run_id, match["game_id"], row["participant_id"],
                        row["total_score"], row["match_rank"],
                        row["components_json"], row["observations_json"],
                    ),
                )
            conn.execute(
                "UPDATE matches SET active_score_run_id = ? WHERE game_id = ?",
                (run_id, match["game_id"]),
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

    def participant_puuids(self, game_id: int) -> set[str]:
        """Stored participant PUUIDs for a game, ignoring blank identities.

        Used to cross-validate an upgrade timeline source (e.g. Match-V5)
        against the identity RuneSync already stored for this game_id from
        LCU ingestion, independent of how that upgrade source resolved
        platform/region routing.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT puuid FROM participants WHERE game_id = ?", (game_id,),
            ).fetchall()
        return {row["puuid"] for row in rows if row["puuid"]}

    def get_match(self, game_id: int) -> Optional[dict]:
        """Return the bare `matches` row, or None if unknown.

        Unlike `get_report`, this never requires an active score run --
        Score v2 feature extraction (score_features.py) runs before any
        score exists.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM matches WHERE game_id = ?", (game_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_participants(self, game_id: int) -> list[dict]:
        """Return raw participant rows for a game, no score join required.

        Unlike `get_report`, this is available as soon as `save_report` has
        ingested a match -- Score v2 feature extraction runs before any
        score exists.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM participants
                WHERE game_id = ? ORDER BY participant_id
                """,
                (game_id,),
            ).fetchall()
        participants = []
        for row in rows:
            item = dict(row)
            item["items"] = json.loads(item.pop("items_json"))
            participants.append(item)
        return participants

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
                INSERT INTO matches (
                    game_id, queue_id, map_id, game_mode, game_creation,
                    game_creation_date, duration, patch, local_participant_id,
                    local_win, local_champion_id, local_champion_name, local_role,
                    score_model_version, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    queue_id = excluded.queue_id,
                    map_id = excluded.map_id,
                    game_mode = excluded.game_mode,
                    game_creation = excluded.game_creation,
                    game_creation_date = excluded.game_creation_date,
                    duration = excluded.duration,
                    patch = excluded.patch,
                    local_participant_id = excluded.local_participant_id,
                    local_win = excluded.local_win,
                    local_champion_id = excluded.local_champion_id,
                    local_champion_name = excluded.local_champion_name,
                    local_role = excluded.local_role,
                    imported_at = excluded.imported_at
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
                    ON CONFLICT(game_id, participant_id) DO UPDATE SET
                        puuid = excluded.puuid,
                        summoner_name = excluded.summoner_name,
                        champion_id = excluded.champion_id,
                        champion_name = excluded.champion_name,
                        team_id = excluded.team_id,
                        role = excluded.role,
                        win = excluded.win,
                        kills = excluded.kills,
                        deaths = excluded.deaths,
                        assists = excluded.assists,
                        gold_earned = excluded.gold_earned,
                        cs = excluded.cs,
                        champion_level = excluded.champion_level,
                        damage_to_champions = excluded.damage_to_champions,
                        damage_to_objectives = excluded.damage_to_objectives,
                        damage_to_turrets = excluded.damage_to_turrets,
                        damage_taken = excluded.damage_taken,
                        damage_mitigated = excluded.damage_mitigated,
                        healing = excluded.healing,
                        vision_score = excluded.vision_score,
                        wards_placed = excluded.wards_placed,
                        wards_killed = excluded.wards_killed,
                        items_json = excluded.items_json
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
            participant_ids = [player["participant_id"] for player in participants]
            placeholders = ",".join("?" for _ in participant_ids)
            conn.execute(
                f"""
                DELETE FROM participants
                WHERE game_id = ? AND participant_id NOT IN ({placeholders})
                """,
                (match["game_id"], *participant_ids),
            )
            for score in scores:
                conn.execute(
                    """
                    INSERT INTO scores (
                        game_id, participant_id, model_version, total_score,
                        match_rank, components_json, observations_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(game_id, participant_id) DO NOTHING
                    """,
                    (
                        match["game_id"], score["participant_id"],
                        score["model_version"], score["total_score"],
                        score["match_rank"],
                        json.dumps(score["components"], separators=(",", ":")),
                        json.dumps(score["observations"], separators=(",", ":")),
                    ),
                )
            run_id = self._save_score_run(
                conn,
                match["game_id"],
                scores,
                model_version=match["score_model_version"],
                feature_version=match.get("feature_version", "v1"),
                evidence_source=match.get("evidence_source", "aggregate_legacy"),
                calibration_version=match.get("calibration_version", "v1"),
                model_artifact_hash=match.get(
                    "model_artifact_hash", "legacy-inline"
                ),
                input_hash=match.get("input_hash") or self._hash({
                    "match": match,
                    "participants": participants,
                }),
                confidence=match.get("confidence") or {
                    "evidence_quality": "legacy",
                    "coaching_eligible": False,
                },
                activate=False,
            )
            current = conn.execute(
                "SELECT active_score_run_id FROM matches WHERE game_id = ?",
                (match["game_id"],),
            ).fetchone()
            if not current["active_score_run_id"]:
                conn.execute(
                    "UPDATE matches SET active_score_run_id = ? WHERE game_id = ?",
                    (run_id, match["game_id"]),
                )

    def _save_score_run(
            self, conn: sqlite3.Connection, game_id: int, scores: list[dict],
            model_version: int, feature_version: str, evidence_source: str,
            calibration_version: str, model_artifact_hash: str, input_hash: str,
            confidence: dict, activate: bool,
            artifact_model_version: str = "", model_family: str = "legacy") -> int:
        self._validate_score_participants(conn, game_id, scores)
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO score_runs (
                game_id, model_version, feature_version, evidence_source,
                calibration_version, model_artifact_hash,
                artifact_model_version, model_family, input_hash,
                confidence_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', ?)
            """,
            (
            game_id, int(model_version), feature_version, evidence_source,
            calibration_version, model_artifact_hash,
            artifact_model_version, model_family, input_hash,
            self._json(confidence), created_at,
            ),
        )
        run_id = cursor.lastrowid
        if not run_id:
            run_id = conn.execute(
                """
                SELECT id FROM score_runs
                WHERE game_id = ? AND model_version = ?
                  AND feature_version = ? AND evidence_source = ?
                  AND calibration_version = ?
                  AND model_artifact_hash = ?
                  AND input_hash = ?
                """,
                (
                    game_id, int(model_version), feature_version,
                    evidence_source, calibration_version,
                    model_artifact_hash, input_hash,
                ),
            ).fetchone()["id"]
        for score in scores:
            conn.execute(
                """
                INSERT OR IGNORE INTO score_results (
                    run_id, game_id, participant_id, total_score, match_rank,
                    components_json, observations_json, evidence_json,
                    score_low, score_high, participant_confidence,
                    rank_confidence, abstain, abstain_reasons_json,
                    coaching_json, coaching_eligible
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, game_id, score["participant_id"],
                    score["total_score"], score["match_rank"],
                    self._json(score.get("components", {})),
                    self._json(score.get("observations", [])),
                    self._json(score.get("evidence", [])),
                    score.get("score_low"), score.get("score_high"),
                    score.get("participant_confidence"),
                    score.get("rank_confidence"),
                    int(bool(score.get("abstain", False))),
                    self._json(score.get("abstain_reasons", [])),
                    self._json(score.get("coaching", {})),
                    int(bool(score.get("coaching_eligible", False))),
                ),
            )
        if activate:
            conn.execute(
                """
                UPDATE matches
                SET active_score_run_id = ?, score_model_version = ?
                WHERE game_id = ?
                """,
                (run_id, int(model_version), game_id),
            )
        return int(run_id)

    @staticmethod
    def _validate_score_participants(
            conn: sqlite3.Connection, game_id: int, scores: list[dict]) -> None:
        expected = {
            int(row["participant_id"])
            for row in conn.execute(
                "SELECT participant_id FROM participants WHERE game_id = ?",
                (game_id,),
            ).fetchall()
        }
        if not expected:
            raise ValueError(f"Unknown game ID {game_id}")
        submitted = [int(score["participant_id"]) for score in scores]
        if len(submitted) != len(set(submitted)):
            raise ValueError("A score run contains duplicate participant results")
        if set(submitted) != expected:
            missing = sorted(expected - set(submitted))
            extra = sorted(set(submitted) - expected)
            raise ValueError(
                f"Score run participants did not match game {game_id} "
                f"(missing={missing}, extra={extra})"
            )

    def save_score_run(
            self, game_id: int, scores: list[dict], model_version: int,
            feature_version: str, evidence_source: str,
            calibration_version: str = "", model_artifact_hash: str = "",
            input_hash: str = "", confidence: Optional[dict] = None,
            activate: bool = True, artifact_model_version: str = "",
            model_family: str = "legacy") -> int:
        if not scores:
            raise ValueError("A score run requires participant results")
        resolved_hash = input_hash or self._hash(scores)
        with self._connect() as conn:
            return self._save_score_run(
                conn, game_id, scores, model_version, feature_version,
                evidence_source, calibration_version, model_artifact_hash,
                resolved_hash, confidence or {}, activate,
                artifact_model_version, model_family,
            )

    def activate_score_run_if_preferred(self, run_id: int) -> bool:
        """Activate `run_id` only when it does not downgrade evidence quality.

        A v2 run always supersedes legacy v1. Stronger evidence tiers supersede
        weaker tiers, and a new run from the same tier may replace an older
        model/calibration. Lower-evidence rescoring remains immutable but does
        not become the active UI result.
        """
        with self._connect() as conn:
            # Take the SQLite write lock before reading the active pointer.
            # Match-V5 and reconciled Live Client refreshes can race on
            # independent threads; a deferred transaction would let both
            # decide against the same stale active run and make the last write
            # win even when it carries weaker evidence.
            conn.execute("BEGIN IMMEDIATE")
            candidate = conn.execute(
                "SELECT * FROM score_runs WHERE id = ?", (int(run_id),),
            ).fetchone()
            if not candidate:
                raise ValueError(f"Unknown score run ID {run_id}")
            match = conn.execute(
                "SELECT active_score_run_id FROM matches WHERE game_id = ?",
                (candidate["game_id"],),
            ).fetchone()
            current_id = match["active_score_run_id"] if match else None
            if current_id == run_id:
                return True
            current = (
                conn.execute(
                    "SELECT * FROM score_runs WHERE id = ?", (current_id,),
                ).fetchone()
                if current_id else None
            )
            candidate_priority = SCORE_EVIDENCE_PRIORITY.get(
                candidate["evidence_source"], -2,
            )
            current_priority = (
                SCORE_EVIDENCE_PRIORITY.get(current["evidence_source"], -2)
                if current else -3
            )
            preferred = (
                current is None
                or candidate_priority > current_priority
                or (
                    candidate_priority == current_priority
                    and int(candidate["model_version"]) >= int(current["model_version"])
                )
            )
            if preferred:
                conn.execute(
                    """
                    UPDATE matches
                    SET active_score_run_id = ?, score_model_version = ?
                    WHERE game_id = ?
                    """,
                    (
                        int(run_id), int(candidate["model_version"]),
                        int(candidate["game_id"]),
                    ),
                )
            return preferred

    def list_score_runs(self, game_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sr.*, sr.id = m.active_score_run_id AS is_active
                FROM score_runs sr
                JOIN matches m ON m.game_id = sr.game_id
                WHERE sr.game_id = ?
                ORDER BY sr.created_at DESC, sr.id DESC
                """,
                (game_id,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["confidence"] = json.loads(item.pop("confidence_json"))
            item["is_active"] = bool(item["is_active"])
            out.append(item)
        return out

    def save_timeline_payload(
            self, game_id: int, source: str, payload: dict,
            schema_version: str = "1", completeness: float = 1.0) -> int:
        if not self.has_game(game_id):
            raise ValueError(f"Unknown game ID {game_id}")
        raw = self._json(payload).encode("utf-8")
        content_hash = hashlib.sha256(raw).hexdigest()
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO timeline_payloads (
                    game_id, source, schema_version, completeness,
                    content_hash, payload_zlib, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    game_id, source, schema_version,
                    max(0.0, min(1.0, float(completeness))),
                    content_hash, zlib.compress(raw, level=9), created_at,
                ),
            )
            if cursor.lastrowid:
                return int(cursor.lastrowid)
            row = conn.execute(
                """
                SELECT id FROM timeline_payloads
                WHERE game_id = ? AND source = ? AND content_hash = ?
                """,
                (game_id, source, content_hash),
            ).fetchone()
        return int(row["id"])

    def get_timeline_payload(
            self, game_id: int, source: Optional[str] = None) -> Optional[dict]:
        where = "WHERE game_id = ?"
        params: list = [game_id]
        if source:
            where += " AND source = ?"
            params.append(source)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM timeline_payloads
                {where}
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                params,
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["payload"] = json.loads(
            zlib.decompress(item.pop("payload_zlib")).decode("utf-8")
        )
        return item

    def has_timeline_payload(self, game_id: int, source: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM timeline_payloads
                WHERE game_id = ? AND source = ? LIMIT 1
                """,
                (game_id, source),
            ).fetchone()
        return row is not None

    def game_ids_missing_timeline(
            self, source: str, limit: int = 100,
            now: Optional[datetime.datetime] = None) -> list[int]:
        safe_limit = max(1, min(int(limit), 1000))
        current = now or datetime.datetime.now(datetime.timezone.utc)
        current_iso = current.astimezone(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.game_id
                FROM matches m
                LEFT JOIN timeline_fetch_attempts a
                  ON a.game_id = m.game_id AND a.source = ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM timeline_payloads t
                    WHERE t.game_id = m.game_id AND t.source = ?
                )
                  AND (a.next_retry_at IS NULL OR a.next_retry_at <= ?)
                ORDER BY COALESCE(a.attempt_count, 0) ASC,
                         COALESCE(a.last_attempted_at, '') ASC,
                         m.game_creation DESC
                LIMIT ?
                """,
                (source, source, current_iso, safe_limit),
            ).fetchall()
        return [int(row["game_id"]) for row in rows]

    def timeline_fetch_due(
            self, game_id: int, source: str,
            now: Optional[datetime.datetime] = None) -> bool:
        """Whether a timeline fetch for (game_id, source) should run now.

        True when no payload is stored yet for this (game_id, source) and
        either no prior attempt was recorded or its backoff window (see
        ``record_timeline_fetch_failure``) has elapsed. This gates a single
        opportunistic, per-match fetch (e.g. an inline Match-V5 upgrade
        attempt right after LCU ingestion) the same way
        ``game_ids_missing_timeline`` gates a bulk backfill scan, so a
        failing match backs off fairly without needing a full table scan
        and without starving other matches of their own retry schedule.
        """
        current = now or datetime.datetime.now(datetime.timezone.utc)
        current_iso = current.astimezone(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            has_payload = conn.execute(
                """
                SELECT 1 FROM timeline_payloads
                WHERE game_id = ? AND source = ? LIMIT 1
                """,
                (game_id, source),
            ).fetchone()
            if has_payload:
                return False
            attempt = conn.execute(
                """
                SELECT next_retry_at FROM timeline_fetch_attempts
                WHERE game_id = ? AND source = ?
                """,
                (game_id, source),
            ).fetchone()
        if attempt is None:
            return True
        return attempt["next_retry_at"] <= current_iso

    def record_timeline_fetch_failure(
            self, game_id: int, source: str, error_kind: str,
            now: Optional[datetime.datetime] = None) -> int:
        current = now or datetime.datetime.now(datetime.timezone.utc)
        current = current.astimezone(datetime.timezone.utc)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT attempt_count FROM timeline_fetch_attempts
                WHERE game_id = ? AND source = ?
                """,
                (game_id, source),
            ).fetchone()
            attempt_count = int(row["attempt_count"]) + 1 if row else 1
            delays = (300, 1800, 10800, 64800, 388800, 604800)
            delay = delays[min(attempt_count - 1, len(delays) - 1)]
            next_retry = current + datetime.timedelta(seconds=delay)
            conn.execute(
                """
                INSERT INTO timeline_fetch_attempts (
                    game_id, source, attempt_count, last_attempted_at,
                    next_retry_at, last_error_kind
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id, source) DO UPDATE SET
                    attempt_count = excluded.attempt_count,
                    last_attempted_at = excluded.last_attempted_at,
                    next_retry_at = excluded.next_retry_at,
                    last_error_kind = excluded.last_error_kind
                """,
                (
                    game_id, source, attempt_count, current.isoformat(),
                    next_retry.isoformat(), error_kind,
                ),
            )
        return attempt_count

    def clear_timeline_fetch_failure(self, game_id: int, source: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM timeline_fetch_attempts
                WHERE game_id = ? AND source = ?
                """,
                (game_id, source),
            )

    # ── live client capture (live_capture_sessions/events/snapshots) ────────
    # Supplemental/fallback local evidence collected while a game is active
    # by live_client.py. `game_id` starts NULL (the Live Client Data API
    # never reports one) and is attached later via
    # reconcile_live_capture_session once the LCU's authoritative game/match
    # ID is known, so there is intentionally no foreign key to `matches`.
    # `last_event_id` starts at -1 (not 0): Riot's EventID sequence itself
    # starts at 0, so the "nothing captured yet" sentinel must sort below
    # every real event ID.

    def start_live_capture_session(
            self, session_id: str, game_id: Optional[int] = None,
            metadata: Optional[dict] = None,
            started_at: Optional[str] = None) -> None:
        now = started_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO live_capture_sessions (
                    session_id, game_id, started_at, ended_at, status,
                    completeness, last_event_id, metadata_json, updated_at
                ) VALUES (?, ?, ?, NULL, 'active', 0.0, -1, ?, ?)
                """,
                (session_id, game_id, now, self._json(metadata or {}), now),
            )

    @staticmethod
    def _live_capture_session_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item

    def get_live_capture_session(self, session_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM live_capture_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._live_capture_session_row(row) if row else None

    def list_live_capture_sessions(self, status: Optional[str] = None) -> list[dict]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT * FROM live_capture_sessions
                    WHERE status = ? ORDER BY started_at
                    """,
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM live_capture_sessions ORDER BY started_at",
                ).fetchall()
        return [self._live_capture_session_row(row) for row in rows]

    def find_resumable_live_capture_session(
            self, game_id: Optional[int]) -> Optional[str]:
        """Return the most recent still-'active' session eligible to resume.

        Eligible means it has no game_id yet, or already carries the same
        one we expect to attach -- either way, continuing it (rather than
        starting a fresh session) is how crash/reconnect recovery works.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT session_id FROM live_capture_sessions
                WHERE status = 'active' AND (game_id IS NULL OR game_id = ?)
                ORDER BY started_at DESC LIMIT 1
                """,
                (game_id,),
            ).fetchone()
        return row["session_id"] if row else None

    def update_live_capture_session(
            self, session_id: str, last_event_id: Optional[int] = None,
            completeness: Optional[float] = None) -> None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        fields = ["updated_at = ?"]
        params: list = [now]
        if last_event_id is not None:
            fields.append("last_event_id = ?")
            params.append(int(last_event_id))
        if completeness is not None:
            fields.append("completeness = ?")
            params.append(max(0.0, min(1.0, float(completeness))))
        params.append(session_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE live_capture_sessions SET {', '.join(fields)} "
                "WHERE session_id = ?",
                params,
            )

    def finalize_live_capture_session(
            self, session_id: str, status: str,
            ended_at: Optional[str] = None) -> None:
        now = ended_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE live_capture_sessions
                SET status = ?, ended_at = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (status, now, now, session_id),
            )

    def reconcile_live_capture_session(self, session_id: str, game_id: int) -> str:
        """Attach the authoritative LCU game ID to a capture session.

        Returns 'reconciled' when the session had no game_id yet (or already
        matched), or 'mismatch' when it was already attached to a
        *different* game_id -- in that case the session is relabeled
        explicitly instead of silently overwritten, since something about
        the earlier reconciliation (or this one) is wrong.
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT game_id FROM live_capture_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown live capture session {session_id}")
            existing_game_id = row["game_id"]
            if existing_game_id is not None and int(existing_game_id) != int(game_id):
                conn.execute(
                    """
                    UPDATE live_capture_sessions
                    SET status = 'reconciliation_mismatch', updated_at = ?
                    WHERE session_id = ?
                    """,
                    (now, session_id),
                )
                return "mismatch"
            conn.execute(
                """
                UPDATE live_capture_sessions
                SET game_id = ?, updated_at = ? WHERE session_id = ?
                """,
                (game_id, now, session_id),
            )
        return "reconciled"

    def record_live_capture_events(
            self, session_id: str, events: list[dict]) -> int:
        """Insert new events, deduplicated by (session_id, event_id). Returns
        the number actually inserted (already-seen events are ignored)."""
        if not events:
            return 0
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        inserted = 0
        with self._connect() as conn:
            for event in events:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO live_capture_events (
                        session_id, event_id, event_time, event_type, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session_id, int(event["event_id"]),
                        float(event["event_time"]), str(event["event_type"]),
                        self._json(event["payload"]),
                    ),
                )
                if cursor.rowcount:
                    inserted += 1
            conn.execute(
                "UPDATE live_capture_sessions SET updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
        return inserted

    def get_live_capture_events(self, session_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, event_time, event_type, payload_json
                FROM live_capture_events WHERE session_id = ? ORDER BY event_id
                """,
                (session_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"], "event_time": row["event_time"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def record_live_capture_snapshot(
            self, session_id: str, snapshot_time: float, payload: dict) -> bool:
        """Persist a snapshot, skipping it if identical to the session's most
        recent one (storage budget: avoid paying for no-op repeats), and
        deduplicating by (session_id, snapshot_time) as a concurrency
        backstop. Returns True iff a new row was written."""
        raw = self._json(payload).encode("utf-8")
        content_hash = self._hash_bytes(raw)
        with self._connect() as conn:
            last = conn.execute(
                """
                SELECT content_hash FROM live_capture_snapshots
                WHERE session_id = ? ORDER BY snapshot_time DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if last and last["content_hash"] == content_hash:
                return False
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO live_capture_snapshots (
                    session_id, snapshot_time, content_hash, payload_zlib
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    session_id, float(snapshot_time), content_hash,
                    zlib.compress(raw, level=9),
                ),
            )
        return bool(cursor.rowcount)

    def get_live_capture_snapshots(self, session_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT snapshot_time, payload_zlib FROM live_capture_snapshots
                WHERE session_id = ? ORDER BY snapshot_time
                """,
                (session_id,),
            ).fetchall()
        return [
            {
                "snapshot_time": row["snapshot_time"],
                "payload": json.loads(
                    zlib.decompress(row["payload_zlib"]).decode("utf-8")
                ),
            }
            for row in rows
        ]

    def save_feature_set(
            self, game_id: int, feature_version: str, evidence_source: str,
            features: dict, evidence: Optional[list] = None,
            input_hash: str = "") -> int:
        if not self.has_game(game_id):
            raise ValueError(f"Unknown game ID {game_id}")
        resolved_hash = input_hash or self._hash(features)
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO feature_sets (
                    game_id, feature_version, evidence_source, input_hash,
                    features_json, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    game_id, feature_version, evidence_source, resolved_hash,
                    self._json(features), self._json(evidence or []), created_at,
                ),
            )
            if cursor.lastrowid:
                return int(cursor.lastrowid)
            row = conn.execute(
                """
                SELECT id FROM feature_sets
                WHERE game_id = ? AND feature_version = ?
                  AND evidence_source = ? AND input_hash = ?
                """,
                (game_id, feature_version, evidence_source, resolved_hash),
            ).fetchone()
        return int(row["id"])

    def get_feature_set(
            self, game_id: int, feature_version: Optional[str] = None,
            evidence_source: Optional[str] = None) -> Optional[dict]:
        """Read back the newest saved feature set for `game_id`.

        Narrow read accessor for `save_feature_set` -- Score v2
        training/evaluation tooling needs to load persisted feature sets
        without duplicating the `feature_sets` schema here. Returns the
        most recent matching row (by `created_at`/`id`), or None if no
        feature set has been saved for this game (and, if supplied,
        `feature_version`/`evidence_source`).
        """
        where = "WHERE game_id = ?"
        params: list = [game_id]
        if feature_version:
            where += " AND feature_version = ?"
            params.append(feature_version)
        if evidence_source:
            where += " AND evidence_source = ?"
            params.append(evidence_source)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM feature_sets
                {where}
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                params,
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["features"] = json.loads(item.pop("features_json"))
        item["evidence"] = json.loads(item.pop("evidence_json"))
        return item

    def list_feature_sets(self, game_id: Optional[int] = None) -> list[dict]:
        """List saved feature sets, newest first, optionally filtered by game."""
        where = ""
        params: list = []
        if game_id is not None:
            where = "WHERE game_id = ?"
            params.append(game_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM feature_sets
                {where}
                ORDER BY created_at DESC, id DESC
                """,
                params,
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["features"] = json.loads(item.pop("features_json"))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            results.append(item)
        return results

    def list_recent_local_feature_blocks(
            self, game_id: int, feature_version: str,
            evidence_source: str, limit: int = 5,
            min_completeness: float = 0.0) -> list[dict]:
        """Return prior same-role local feature blocks for recurrence checks.

        Only games created before `game_id` are eligible, preventing a
        historical backfill from reading future matches. At most one newest
        feature set per game is returned, and abstained games are excluded.
        """
        current = self.get_match(game_id)
        if current is None:
            raise ValueError(f"Unknown game ID {game_id}")
        safe_limit = max(1, min(int(limit), 20))
        batch_size = max(20, safe_limit * 2)
        blocks = []
        cursor_creation = int(current["game_creation"])
        cursor_game_id = 2 ** 63 - 1
        with self._connect() as conn:
            while len(blocks) < safe_limit:
                rows = conn.execute(
                    """
                    WITH candidate_matches AS (
                        SELECT game_id, local_participant_id, game_creation
                        FROM matches
                        WHERE game_id != ?
                          AND local_role = ?
                          AND game_creation < ?
                          AND (
                              game_creation < ?
                              OR (game_creation = ? AND game_id < ?)
                          )
                        ORDER BY game_creation DESC, game_id DESC
                        LIMIT ?
                    )
                    SELECT
                        m.game_id,
                        m.local_participant_id,
                        m.game_creation,
                        fs.features_json
                    FROM candidate_matches m
                    LEFT JOIN feature_sets fs ON fs.id = (
                        SELECT candidate.id
                        FROM feature_sets candidate
                        WHERE candidate.game_id = m.game_id
                          AND candidate.feature_version = ?
                          AND candidate.evidence_source = ?
                        ORDER BY candidate.created_at DESC, candidate.id DESC
                        LIMIT 1
                    )
                    ORDER BY m.game_creation DESC, m.game_id DESC
                    """,
                    (
                        int(game_id), current["local_role"],
                        int(current["game_creation"]),
                        cursor_creation, cursor_creation, cursor_game_id,
                        batch_size, feature_version, evidence_source,
                    ),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    if row["features_json"] is None:
                        continue
                    features = json.loads(row["features_json"])
                    if features.get("abstain"):
                        continue
                    completeness = float(
                        features.get("chosen_source_completeness") or 0.0
                    )
                    if completeness < float(min_completeness):
                        continue
                    block = (features.get("participants") or {}).get(
                        str(row["local_participant_id"]),
                    )
                    if isinstance(block, dict):
                        blocks.append(block)
                    if len(blocks) >= safe_limit:
                        break
                last_row = rows[-1]
                cursor_creation = int(last_row["game_creation"])
                cursor_game_id = int(last_row["game_id"])
                if len(rows) < batch_size:
                    break
        return blocks

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
                SELECT ROUND(AVG(r.total_score), 1) AS average_score,
                       MIN(r.match_rank) AS best_rank,
                       ROUND(AVG(r.match_rank), 1) AS average_rank
                FROM matches m
                JOIN score_results r
                  ON r.run_id = m.active_score_run_id
                 AND r.participant_id = m.local_participant_id
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
                       r.total_score, r.match_rank, r.score_low, r.score_high,
                       r.participant_confidence, r.rank_confidence,
                       r.abstain, r.abstain_reasons_json,
                       r.coaching_json, r.coaching_eligible,
                       sr.evidence_source, sr.feature_version,
                       sr.calibration_version, sr.model_artifact_hash,
                       sr.artifact_model_version, sr.model_family,
                       sr.model_version AS active_model_version,
                       sr.confidence_json,
                       (SELECT MIN(r2.match_rank)
                          FROM score_results r2
                          JOIN participants p2
                            ON p2.game_id = m.game_id
                           AND p2.participant_id = r2.participant_id
                         WHERE r2.run_id = sr.id
                           AND p2.team_id = p.team_id
                           AND r2.participant_id <> m.local_participant_id
                       ) AS team_best_other_rank
                FROM matches m
                JOIN participants p
                  ON p.game_id = m.game_id
                 AND p.participant_id = m.local_participant_id
                JOIN score_runs sr
                  ON sr.id = m.active_score_run_id
                JOIN score_results r
                  ON r.run_id = sr.id
                 AND r.participant_id = m.local_participant_id
                ORDER BY m.game_creation DESC
                LIMIT ? OFFSET ?
                """,
                (safe_limit, safe_offset),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["score_confidence"] = json.loads(item.pop("confidence_json"))
            item["abstain"] = bool(item["abstain"])
            item["abstain_reasons"] = json.loads(
                item.pop("abstain_reasons_json"),
            )
            item["coaching"] = json.loads(item.pop("coaching_json"))
            item["coaching_eligible"] = bool(item["coaching_eligible"])
            out.append(item)
        return out

    def get_report(self, game_id: int) -> Optional[dict]:
        return self.get_score_run_report(game_id)

    def get_score_run_report(
            self, game_id: int, run_id: Optional[int] = None) -> Optional[dict]:
        """Return a report for one immutable run without changing activation.

        When ``run_id`` is omitted this is the active report used by the UI.
        Offline shadow tooling supplies an explicit historical run so it can
        compare v1 and v2 without moving ``matches.active_score_run_id``.
        """
        with self._connect() as conn:
            match = conn.execute(
                "SELECT * FROM matches WHERE game_id = ?", (game_id,),
            ).fetchone()
            if not match:
                return None
            resolved_run_id = (
                int(run_id) if run_id is not None
                else match["active_score_run_id"]
            )
            if resolved_run_id is None:
                return {"match": dict(match), "participants": []}
            players = conn.execute(
                """
                SELECT p.*, sr.model_version, sr.feature_version,
                       sr.evidence_source, sr.calibration_version,
                       sr.model_artifact_hash, sr.input_hash,
                       sr.artifact_model_version, sr.model_family,
                       sr.confidence_json, r.total_score, r.match_rank,
                       r.components_json, r.observations_json, r.evidence_json,
                       r.score_low, r.score_high, r.participant_confidence,
                       r.rank_confidence, r.abstain, r.abstain_reasons_json,
                       r.coaching_json,
                       r.coaching_eligible
                FROM participants p
                JOIN matches m ON m.game_id = p.game_id
                JOIN score_runs sr
                  ON sr.id = ?
                 AND sr.game_id = m.game_id
                JOIN score_results r
                  ON r.run_id = sr.id
                 AND r.participant_id = p.participant_id
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
                (resolved_run_id, game_id),
            ).fetchall()

        participants = []
        for row in players:
            item = dict(row)
            item["items"] = json.loads(item.pop("items_json"))
            item["components"] = json.loads(item.pop("components_json"))
            item["observations"] = json.loads(item.pop("observations_json"))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            item["score_confidence"] = json.loads(item.pop("confidence_json"))
            item["abstain"] = bool(item["abstain"])
            item["abstain_reasons"] = json.loads(
                item.pop("abstain_reasons_json"),
            )
            item["coaching"] = json.loads(item.pop("coaching_json"))
            item["coaching_eligible"] = bool(item["coaching_eligible"])
            participants.append(item)
        return {"match": dict(match), "participants": participants}
