"""Supplemental/fallback Live Client Data collector for DAEMON Score v2.

Per the DAEMON Score v2 evidence hierarchy (see the vault decision
"Promote LCU post-game timelines into DAEMON Score v2 evidence hierarchy"),
this module is evidence path (3): a local, crash-resilient, sub-minute
capture of the League game client's Live Client Data HTTP API, consulted
when the post-game LCU historical timeline (timeline_provider.py) is
unavailable, or layered alongside it for higher-frequency state. It must
never become the sole source of local timeline evidence.

The Live Client Data API (https://127.0.0.1:2999/liveclientdata/*) only
exists on the local machine while a game is active -- there is no
authentication and no game/match ID in its payloads. Two consequences drive
this module's design:

  * The endpoint being unreachable is the *expected* steady state whenever
    no game is running, not an error -- callers must treat it as routine and
    retryable, never log-spam it, and never fabricate a "successful" empty
    capture when it is down or returns something malformed.
  * Because it never reports a game/match ID, a capture session must be
    reconciled after the fact against the authoritative ID the LCU exposes
    (via ChampSelectMonitor/MatchHistoryService's gameflow polling). Until
    reconciled, or if reconciliation ever produces a *different* ID than the
    session already carries, the session is labeled explicitly rather than
    silently trusted. Reconciliation always targets the *exact* session
    that was stopped for that game -- bridge.py threads the session_id
    returned by LiveCaptureManager.stop() through post-game ingestion into
    reconcile(). There is deliberately no "most recently started session"
    lookup: a new game can start while the previous game's post-game
    ingestion is still resolving asynchronously, and guessing "the latest
    session" would then mislabel the wrong one.

It also exposes materially asymmetric data: `activePlayer` carries full
runes, ability ranks, champion stats and exact current gold for the local
player only -- Riot deliberately withholds those for the other nine
participants to prevent opponent-tracking. Persisting that as if it were
comparable per-player evidence would silently bias any score that reads it,
so this module always keeps it in a separately labeled `active_player`
section and never lets it leak into the shared, cross-player `players` list.
"""

from __future__ import annotations

import json
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

DEFAULT_BASE_URL = "https://127.0.0.1:2999"

# ── Cadence & budget constants ──────────────────────────────────────────────
# Profiled against tests/fixtures/live_client_all_game_data.json, a
# representative 10-player allgamedata payload (~11 KB raw JSON). See
# tests/test_live_client.py::test_storage_budget_bounded_for_a_full_game and
# ::test_poll_cpu_cost_is_negligible for the measurements backing these
# defaults:
#   * ~11 KB raw / ~1.6 KB zlib-compressed per snapshot.
#   * poll_once() over a fixture payload costs well under 1 ms of CPU, so the
#     loop is I/O- and sleep-bound, not CPU-bound, at any of these cadences.
# Polling every 2s keeps event-boundary latency low (events are cheap -- a
# few dozen per game, deduplicated by EventID) while persisting a throttled
# snapshot only every 15s bounds storage independently of poll cadence: a
# 45-minute game polls ~1350 times but stores <=180 snapshot rows, well
# under ~300 KB compressed.
POLL_INTERVAL_SECONDS = 2.0
SNAPSHOT_INTERVAL_SECONDS = 15.0
REQUEST_TIMEOUT_SECONDS = 2.0
# Bounded logging: only the first few consecutive occurrences of a given
# failure class are logged per session; the rest are counted, not printed.
MAX_CONSECUTIVE_LOGS = 3
UNAVAILABLE_BACKOFF_SECONDS = 5.0
# ~150 misses at the 5s backoff is ~12.5 minutes of a fully unreachable
# endpoint -- comfortably longer than any real reconnect blip, so treat it as
# the game/client process having gone away and end the session as partial.
MAX_UNAVAILABLE_BEFORE_PARTIAL = 150


class LiveClientDataError(Exception):
    """Base class for Live Client Data collection failures."""


class LiveClientUnavailableError(LiveClientDataError):
    """The local Live Client Data endpoint could not be reached.

    Expected whenever no game is loaded -- the game client's local HTTPS
    server only exists while a match is active -- so this is retryable, not
    fatal.
    """


class LiveClientMalformedPayloadError(LiveClientDataError):
    """The endpoint responded, but the payload was structurally invalid."""


# Fields the Live Client Data API exposes ONLY for the local player.
ACTIVE_PLAYER_ONLY_KEYS = frozenset({
    "abilities", "championStats", "currentGold", "fullRunes",
})

# Fields captured for all ten players and safe to compare across the roster.
COMPARABLE_PLAYER_KEYS = (
    "riotId", "riotIdGameName", "riotIdTagLine", "summonerName",
    "championName", "team", "position", "level", "isBot", "isDead",
    "respawnTimer", "items", "scores", "summonerSpells", "runes",
)


class LiveClientDataClient:
    """Thin, dependency-free wrapper over the local Live Client Data API."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = REQUEST_TIMEOUT_SECONDS):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def _get(self, path: str) -> dict:
        url = self._base_url + path
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(
                    req, context=self._ssl_ctx, timeout=self._timeout) as resp:
                raw = resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise LiveClientUnavailableError(
                f"Live Client Data endpoint not reachable: {exc}"
            ) from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LiveClientMalformedPayloadError(
                f"Live Client Data returned invalid JSON: {exc}"
            ) from exc

    def get_all_game_data(self) -> dict:
        data = self._get("/liveclientdata/allgamedata")
        _validate_all_game_data(data)
        return data


def _validate_all_game_data(data) -> None:
    if not isinstance(data, dict):
        raise LiveClientMalformedPayloadError(
            "Live Client Data payload was not a JSON object."
        )
    active_player = data.get("activePlayer")
    all_players = data.get("allPlayers")
    events = data.get("events")
    game_data = data.get("gameData")
    if not isinstance(active_player, dict) or not active_player:
        raise LiveClientMalformedPayloadError(
            "Live Client Data was missing activePlayer."
        )
    if not isinstance(all_players, list) or not all_players:
        raise LiveClientMalformedPayloadError(
            "Live Client Data was missing allPlayers."
        )
    for player in all_players:
        if not isinstance(player, dict) or not player.get("championName"):
            raise LiveClientMalformedPayloadError(
                "Live Client Data contained an invalid player entry."
            )
    if not isinstance(events, dict) or not isinstance(events.get("Events"), list):
        raise LiveClientMalformedPayloadError(
            "Live Client Data was missing an events list."
        )
    if not isinstance(game_data, dict) or not isinstance(
            game_data.get("gameTime"), (int, float)):
        raise LiveClientMalformedPayloadError(
            "Live Client Data was missing gameData.gameTime."
        )


def normalize_snapshot(all_game_data: dict) -> dict:
    """Split a raw allgamedata payload into labeled active/comparable data.

    The returned `players` list is safe to compare across all ten
    participants. `active_player` is always tagged `active_player_only` and
    must never be merged back into `players` -- see the module docstring.
    """
    active_player = dict(all_game_data.get("activePlayer") or {})
    active_player["active_player_only"] = True
    players = []
    for player in all_game_data.get("allPlayers") or []:
        if not isinstance(player, dict):
            continue
        comparable = {
            key: player[key] for key in COMPARABLE_PLAYER_KEYS if key in player
        }
        # Defensive: a future Riot payload change must never let an
        # active-player-only key silently become "comparable" evidence.
        for key in ACTIVE_PLAYER_ONLY_KEYS:
            comparable.pop(key, None)
        players.append(comparable)
    game_data = all_game_data.get("gameData") or {}
    return {
        "game_time": float(game_data.get("gameTime", 0.0)),
        "game_mode": game_data.get("gameMode"),
        "active_player": active_player,
        "players": players,
    }


def extract_events(all_game_data: dict) -> list[dict]:
    """Normalize the (cumulative) `events.Events` list to `{event_id, ...}`."""
    raw_events = ((all_game_data.get("events") or {}).get("Events")) or []
    normalized = []
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        event_id = event.get("EventID")
        if not isinstance(event_id, int):
            continue
        normalized.append({
            "event_id": event_id,
            "event_time": float(event.get("EventTime", 0.0)),
            "event_type": str(event.get("EventName", "Unknown")),
            "payload": event,
        })
    return normalized


def estimate_completeness(all_game_data: dict) -> float:
    """Fraction of the ten expected players carrying a usable scores block."""
    players = all_game_data.get("allPlayers") or []
    if not players:
        return 0.0
    usable = sum(
        1 for player in players
        if isinstance(player, dict) and isinstance(player.get("scores"), dict)
    )
    return round(min(usable, 10) / 10.0, 4)


@dataclass
class CaptureStats:
    polls: int = 0
    events_captured: int = 0
    snapshots_captured: int = 0
    unavailable_count: int = 0
    malformed_count: int = 0
    consecutive_unavailable: int = 0
    # Transient local storage failures (disk full, WAL contention beyond
    # SQLite's own busy_timeout, etc.) -- distinct from endpoint problems.
    # Any of these means evidence for that poll was dropped or delayed, so
    # finish() marks the session partial rather than "completed" even if it
    # otherwise captured plenty of data.
    store_errors: int = 0
    consecutive_store_errors: int = 0


class LiveCaptureSession:
    """Owns the lifecycle of a single Live Client Data capture session."""

    def __init__(self, store, client: Optional[LiveClientDataClient] = None,
                 on_log: Optional[Callable[[str, str], None]] = None,
                 poll_interval: float = POLL_INTERVAL_SECONDS,
                 snapshot_interval: float = SNAPSHOT_INTERVAL_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.store = store
        self.client = client or LiveClientDataClient()
        self.on_log = on_log or (lambda message, tag="info": None)
        self.poll_interval = poll_interval
        self.snapshot_interval = snapshot_interval
        self._clock = clock
        self.session_id: Optional[str] = None
        self.expected_game_id: Optional[int] = None
        # -1, not 0: Riot's EventID sequence itself starts at 0, so the
        # "nothing captured yet" sentinel must sort below every real ID.
        self._last_event_id = -1
        self._last_snapshot_at: Optional[float] = None
        self._stop_event = threading.Event()
        self.stats = CaptureStats()

    def start(self, expected_game_id: Optional[int] = None,
              session_id: Optional[str] = None) -> str:
        """Begin (or, given a matching existing session, resume) capture."""
        resumed = self.store.get_live_capture_session(session_id) if session_id else None
        if resumed and resumed["status"] == "active":
            self.session_id = session_id
            self._last_event_id = int(resumed["last_event_id"])
            self.expected_game_id = resumed["game_id"] or expected_game_id
            self.on_log(f"Live capture resumed session {session_id}.", "info")
        else:
            self.session_id = uuid.uuid4().hex
            self.expected_game_id = expected_game_id
            self.store.start_live_capture_session(
                self.session_id, game_id=expected_game_id,
                metadata={"expected_game_id": expected_game_id},
            )
            self.on_log(f"Live capture started session {self.session_id}.", "info")
        self._stop_event = threading.Event()
        return self.session_id

    def request_stop(self) -> None:
        self._stop_event.set()

    def poll_once(self) -> bool:
        """Run one poll iteration. Returns True iff a sample was captured."""
        self.stats.polls += 1
        try:
            data = self.client.get_all_game_data()
        except LiveClientUnavailableError as exc:
            self.stats.unavailable_count += 1
            self.stats.consecutive_unavailable += 1
            if self.stats.consecutive_unavailable <= MAX_CONSECUTIVE_LOGS:
                self.on_log(f"Live capture endpoint unavailable: {exc}", "info")
            return False
        except LiveClientMalformedPayloadError as exc:
            self.stats.malformed_count += 1
            if self.stats.malformed_count <= MAX_CONSECUTIVE_LOGS:
                self.on_log(f"Live capture received a malformed payload: {exc}", "warn")
            return False

        self.stats.consecutive_unavailable = 0
        ok = True
        events = extract_events(data)
        new_events = [e for e in events if e["event_id"] > self._last_event_id]
        if new_events:
            try:
                inserted = self.store.record_live_capture_events(
                    self.session_id, new_events,
                )
            except (sqlite3.Error, OSError) as exc:
                ok = self._note_store_error(exc, "persist events")
            else:
                # Only advance the watermark on success -- on failure the
                # same (still "new") events are retried next poll, since
                # Live Client Data's events list is cumulative and
                # record_live_capture_events is idempotent per event_id.
                self._last_event_id = max(e["event_id"] for e in new_events)
                self.stats.events_captured += inserted

        now = self._clock()
        if self._last_snapshot_at is None or (
                now - self._last_snapshot_at) >= self.snapshot_interval:
            # Advance the throttle watermark regardless of write outcome --
            # a failed snapshot write is not retried immediately (that
            # specific moment in time is lost), only at the next interval,
            # so a stuck store can't turn into a tight retry loop.
            self._last_snapshot_at = now
            snapshot = normalize_snapshot(data)
            try:
                if self.store.record_live_capture_snapshot(
                        self.session_id, snapshot["game_time"], snapshot):
                    self.stats.snapshots_captured += 1
            except (sqlite3.Error, OSError) as exc:
                ok = self._note_store_error(exc, "persist a snapshot")

        try:
            self.store.update_live_capture_session(
                self.session_id, last_event_id=self._last_event_id,
                completeness=estimate_completeness(data),
            )
        except (sqlite3.Error, OSError) as exc:
            ok = self._note_store_error(exc, "update session progress")

        if ok:
            self.stats.consecutive_store_errors = 0
        return ok

    def _note_store_error(self, exc: Exception, action: str) -> bool:
        """Record a transient local-storage failure without letting it kill
        the capture loop. Bounded-logged like endpoint failures; tracked
        separately in stats so finish() can mark the session partial even
        if earlier polls captured plenty of data."""
        self.stats.store_errors += 1
        self.stats.consecutive_store_errors += 1
        if self.stats.consecutive_store_errors <= MAX_CONSECUTIVE_LOGS:
            self.on_log(f"Live capture failed to {action}: {exc}", "warn")
        return False

    def run(self) -> None:
        """Blocking poll loop; run on a daemon thread until request_stop()."""
        while not self._stop_event.is_set():
            ok = self.poll_once()
            if not ok and self.stats.consecutive_unavailable >= MAX_UNAVAILABLE_BEFORE_PARTIAL:
                self.on_log(
                    "Live capture endpoint unavailable for too long; ending session.",
                    "warn",
                )
                break
            wait = self.poll_interval if ok else UNAVAILABLE_BACKOFF_SECONDS
            self._stop_event.wait(wait)

    def finish(self, status: Optional[str] = None) -> Optional[str]:
        """Finalize the session with a terminal status, inferred if omitted."""
        if not self.session_id:
            return None
        if status is None:
            empty_capture = (
                self.stats.snapshots_captured == 0
                and self.stats.events_captured == 0
            )
            if empty_capture and self.stats.store_errors > 0:
                # The capture is empty because local storage rejected
                # everything we tried to write, not because there was no
                # game activity to record -- that is the authoritative
                # failure and must not be hidden behind the generic
                # "no data" label.
                status = "partial_storage_errors"
            elif empty_capture:
                status = "partial_no_data"
            elif self.stats.consecutive_unavailable >= MAX_UNAVAILABLE_BEFORE_PARTIAL:
                status = "partial_endpoint_lost"
            elif self.stats.store_errors > 0:
                # Evidence was dropped or delayed by local storage failures
                # at some point -- never silently report "completed" here,
                # even though some data was captured.
                status = "partial_storage_errors"
            else:
                status = "completed"
        self.store.finalize_live_capture_session(self.session_id, status=status)
        return status

    def reconcile(self, authoritative_game_id: int) -> str:
        if not self.session_id:
            return "no_session"
        return self.store.reconcile_live_capture_session(
            self.session_id, authoritative_game_id,
        )


class LiveCaptureManager:
    """Thread-owning facade bridge.py uses to start/stop/reconcile capture.

    Only one capture session runs at a time (RuneSync monitors a single
    League client), so this manager also sweeps any *other* session left in
    an 'active' state -- always the residue of a previous process crash --
    every time a new session starts or an explicit recovery pass runs.
    """

    def __init__(self, store, client_factory: Callable[[], LiveClientDataClient] = LiveClientDataClient,
                 on_log: Optional[Callable[[str, str], None]] = None,
                 session_factory: Callable[..., LiveCaptureSession] = LiveCaptureSession):
        self.store = store
        self._client_factory = client_factory
        self.on_log = on_log or (lambda message, tag="info": None)
        self._session_factory = session_factory
        self._session: Optional[LiveCaptureSession] = None
        self._thread: Optional[threading.Thread] = None
        # Sessions whose finish() call raised and hasn't yet been retried
        # successfully, keyed by session_id -> (session, requested status).
        # Kept here (not forgotten) so a subsequent start()/stop() can retry
        # finalizing them, and so start() never resumes/mixes a brand new
        # game's capture into a session whose terminal status hasn't landed.
        self._pending_finalize: dict[str, tuple[LiveCaptureSession, Optional[str]]] = {}
        self._lock = threading.Lock()

    def recover_stale_sessions(self) -> int:
        """Finalize any session left 'active' by a previous, crashed process.

        Excludes sessions currently held in _pending_finalize: those are
        being retried locally by *this* process and are not crash residue
        from a previous one, so relabeling them here would hide the real
        status and race with the in-progress retry.
        """
        with self._lock:
            stuck_ids = set(self._pending_finalize.keys())
        stale = self.store.list_live_capture_sessions(status="active")
        recovered = [s for s in stale if s["session_id"] not in stuck_ids]
        for session in recovered:
            self.store.finalize_live_capture_session(
                session["session_id"], status="partial_process_restart",
            )
        if recovered:
            self.on_log(
                f"Recovered {len(recovered)} interrupted live capture session(s).",
                "info",
            )
        return len(recovered)

    def _finalize(self, session: LiveCaptureSession,
                  status: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        try:
            final_status = session.finish(status=status)
        except (sqlite3.Error, OSError) as exc:
            with self._lock:
                self._pending_finalize[session.session_id] = (session, status)
            self.on_log(
                f"Live capture session {session.session_id} could not be "
                f"finalized ({exc}); it will be retried, and will not be "
                "resumed or mixed into a new game's capture until it is.",
                "warn",
            )
            return session.session_id, None
        with self._lock:
            self._pending_finalize.pop(session.session_id, None)
        return session.session_id, final_status

    def _retry_pending_finalizes(self) -> dict[str, str]:
        """Retry any sessions whose finish() previously failed.

        Returns {session_id: resolved_status} for sessions that finalized
        successfully during this pass, so a caller (e.g. stop()) that just
        added a session to _pending_finalize moments ago can learn whether
        this same immediate retry already resolved it, instead of reporting
        a stale "still pending" result while the DB is already terminal.
        """
        with self._lock:
            pending = list(self._pending_finalize.values())
        resolved: dict[str, str] = {}
        for pending_session, pending_status in pending:
            session_id, final_status = self._finalize(pending_session, pending_status)
            if final_status is not None:
                resolved[session_id] = final_status
        return resolved

    def start(self, expected_game_id: Optional[int] = None) -> Optional[str]:
        with self._lock:
            if self._session is not None:
                return self._session.session_id
        # Retry any session stuck failing to finalize before deciding what
        # is safe to resume/sweep -- outside the lock, since this does I/O.
        self._retry_pending_finalizes()
        with self._lock:
            # Re-check: another start() may have run while the lock above
            # was released for the retry pass.
            if self._session is not None:
                return self._session.session_id
            stuck_ids = set(self._pending_finalize.keys())
            resumable_id = self.store.find_resumable_live_capture_session(expected_game_id)
            if resumable_id in stuck_ids:
                # Its terminal status still hasn't landed -- never resume it
                # into a different game; treat it purely as crash residue
                # once it does finalize.
                resumable_id = None
            for stale in self.store.list_live_capture_sessions(status="active"):
                if stale["session_id"] != resumable_id and stale["session_id"] not in stuck_ids:
                    self.store.finalize_live_capture_session(
                        stale["session_id"], status="partial_process_restart",
                    )
            session = self._session_factory(
                self.store, self._client_factory(), on_log=self.on_log,
            )
            session_id = session.start(
                expected_game_id=expected_game_id, session_id=resumable_id,
            )
            self._session = session
            thread = threading.Thread(target=session.run, daemon=True)
            self._thread = thread
            thread.start()
            return session_id

    def stop(self, status: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        """Stop the current session and finalize it.

        Returns (session_id, terminal_status). terminal_status is None if
        finish() failed -- the session is retained (not forgotten) in
        _pending_finalize and retried here and at the next start(), rather
        than being dropped or left eligible to be resumed by an unrelated
        new game.
        """
        with self._lock:
            session, self._session = self._session, None
            thread, self._thread = self._thread, None
        if not session:
            return None, None
        session.request_stop()
        if thread:
            thread.join(timeout=5)
        result = self._finalize(session, status)
        # Opportunistically retry any *other* session still stuck failing to
        # finalize -- don't make it wait for the next start(). This also
        # covers the just-failed session itself: if its own finalize failed
        # above (result[1] is None) but this immediate retry succeeds (e.g.
        # a one-shot transient DB error), report its resolved status rather
        # than a stale (session_id, None) while the DB row is now terminal.
        resolved = self._retry_pending_finalizes()
        session_id, terminal_status = result
        if terminal_status is None and session_id in resolved:
            result = (session_id, resolved[session_id])
        return result

    def reconcile(self, game_id: Optional[int],
                  session_id: Optional[str] = None) -> Optional[str]:
        """Attach the authoritative LCU game ID to the *exact* session the
        caller stopped for this game (the session_id returned by stop()).

        There is deliberately no "most recently started session" fallback:
        a new game can start (and get its own session) while a previous
        game's post-game ingestion is still resolving asynchronously, and
        guessing "the latest session" would then race and mislabel the
        wrong one. game_id=None (remake/unsupported queue) and
        session_id=None (no capture session is known for this game in this
        process -- e.g. resumed after a full restart post-game) are both
        no-ops, so an unresolved game can never later be attached to a
        different, unrelated game's session.
        """
        if game_id is None or not session_id:
            return None
        return self.store.reconcile_live_capture_session(session_id, game_id)
