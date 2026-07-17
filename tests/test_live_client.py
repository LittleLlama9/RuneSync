import json
import sqlite3
import threading
import time
import zlib
import urllib.error
from pathlib import Path

import pytest

from history_store import HistoryStore
from live_client import (
    ACTIVE_PLAYER_ONLY_KEYS,
    LiveCaptureManager,
    LiveCaptureSession,
    LiveClientDataClient,
    LiveClientMalformedPayloadError,
    LiveClientUnavailableError,
    MAX_UNAVAILABLE_BEFORE_PARTIAL,
    SNAPSHOT_INTERVAL_SECONDS,
    estimate_completeness,
    extract_events,
    normalize_snapshot,
)
import live_client


FIXTURES = Path(__file__).parent / "fixtures"
ALL_GAME_DATA = json.loads(
    (FIXTURES / "live_client_all_game_data.json").read_text()
)


class FakeClient:
    """Stands in for LiveClientDataClient; returns queued results in order,
    repeating the final one once exhausted."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def get_all_game_data(self):
        self.calls += 1
        index = min(self.calls - 1, len(self._results) - 1)
        result = self._results[index]
        if isinstance(result, Exception):
            raise result
        return result


# ── LiveClientDataClient ─────────────────────────────────────────────────────

def test_client_returns_validated_payload(monkeypatch):
    client = LiveClientDataClient()
    monkeypatch.setattr(client, "_get", lambda path: ALL_GAME_DATA)

    data = client.get_all_game_data()

    assert data["allPlayers"][0]["championName"] == "Darius"


def test_client_maps_connection_failures_to_unavailable(monkeypatch):
    client = LiveClientDataClient()

    def boom(req, context, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)

    with pytest.raises(LiveClientUnavailableError):
        client.get_all_game_data()


@pytest.mark.parametrize("broken_payload", [
    {},
    {"activePlayer": {}, "allPlayers": [], "events": {"Events": []}, "gameData": {"gameTime": 1.0}},
    {"activePlayer": {"level": 1}, "allPlayers": [{"championName": "Zed"}], "events": {}, "gameData": {"gameTime": 1.0}},
    {"activePlayer": {"level": 1}, "allPlayers": [{"noChampionName": True}], "events": {"Events": []}, "gameData": {"gameTime": 1.0}},
    {"activePlayer": {"level": 1}, "allPlayers": [{"championName": "Zed"}], "events": {"Events": []}, "gameData": {}},
])
def test_client_rejects_malformed_payloads_explicitly(monkeypatch, broken_payload):
    client = LiveClientDataClient()
    monkeypatch.setattr(client, "_get", lambda path: broken_payload)

    with pytest.raises(LiveClientMalformedPayloadError):
        client.get_all_game_data()


def test_client_rejects_invalid_json(monkeypatch):
    client = LiveClientDataClient()

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"not json"

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Resp())

    with pytest.raises(LiveClientMalformedPayloadError):
        client.get_all_game_data()


# ── normalize_snapshot / extract_events / labeling ──────────────────────────

def test_active_player_only_fields_never_leak_into_comparable_players():
    snapshot = normalize_snapshot(ALL_GAME_DATA)

    assert snapshot["active_player"]["active_player_only"] is True
    assert ACTIVE_PLAYER_ONLY_KEYS <= snapshot["active_player"].keys()
    assert len(snapshot["players"]) == 10
    for player in snapshot["players"]:
        assert not (ACTIVE_PLAYER_ONLY_KEYS & player.keys())
    # The local player's own comparable row must be equally restricted --
    # richer activePlayer data must not be merged in just for them either.
    local = next(p for p in snapshot["players"] if p["championName"] == "Darius")
    assert not (ACTIVE_PLAYER_ONLY_KEYS & local.keys())
    assert local["scores"]["kills"] == 4


def test_normalize_snapshot_keeps_comparable_scoreboard_fields():
    snapshot = normalize_snapshot(ALL_GAME_DATA)
    enemy = next(p for p in snapshot["players"] if p["championName"] == "Garen")

    assert enemy["team"] == "CHAOS"
    assert enemy["isDead"] is True
    assert enemy["respawnTimer"] == 12.4
    assert enemy["scores"]["wardScore"] == 2.0
    assert enemy["position"] == "TOP"


def test_extract_events_ignores_malformed_entries():
    events = extract_events({
        "events": {"Events": [
            {"EventID": 0, "EventName": "GameStart", "EventTime": 0.0},
            {"EventName": "MissingId"},
            "not-a-dict",
        ]},
    })

    assert [e["event_id"] for e in events] == [0]


def test_estimate_completeness_reflects_available_scoreboards():
    full = estimate_completeness(ALL_GAME_DATA)
    partial = estimate_completeness({
        "allPlayers": [{"scores": {}}, {"noScores": True}],
    })

    assert full == 1.0
    assert partial == 0.1


# ── LiveCaptureSession lifecycle ────────────────────────────────────────────

def test_session_start_creates_an_active_row_with_no_game_id(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(store, client=FakeClient([ALL_GAME_DATA]))

    session_id = session.start()

    row = store.get_live_capture_session(session_id)
    assert row["status"] == "active"
    assert row["game_id"] is None


def test_poll_once_persists_events_and_a_snapshot(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(store, client=FakeClient([ALL_GAME_DATA]))
    session.start()

    ok = session.poll_once()

    assert ok is True
    events = store.get_live_capture_events(session.session_id)
    assert len(events) == 5
    snapshots = store.get_live_capture_snapshots(session.session_id)
    assert len(snapshots) == 1
    assert snapshots[0]["payload"]["active_player"]["active_player_only"] is True


def test_poll_once_deduplicates_events_across_polls(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(store, client=FakeClient([ALL_GAME_DATA, ALL_GAME_DATA]))
    session.start()

    session.poll_once()
    session.poll_once()

    events = store.get_live_capture_events(session.session_id)
    assert len(events) == 5  # the second poll re-sent the same cumulative list
    assert session.stats.events_captured == 5


def test_poll_once_throttles_snapshots_independently_of_polling(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    clock = {"t": 0.0}
    session = LiveCaptureSession(
        store, client=FakeClient([ALL_GAME_DATA, ALL_GAME_DATA, ALL_GAME_DATA]),
        clock=lambda: clock["t"],
    )
    session.start()

    session.poll_once()  # t=0, always snapshots the first sample
    clock["t"] = 2.0
    session.poll_once()  # too soon since 2 < SNAPSHOT_INTERVAL_SECONDS
    clock["t"] = SNAPSHOT_INTERVAL_SECONDS + 1
    session.poll_once()  # enough elapsed time -- snapshots again

    snapshots = store.get_live_capture_snapshots(session.session_id)
    assert len(snapshots) == 1  # identical payload content is also deduped by hash
    assert session.stats.polls == 3


def test_poll_once_never_persists_a_success_shaped_empty_capture_on_malformed_payload(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(
        store, client=FakeClient([LiveClientMalformedPayloadError("bad shape")]),
    )
    session.start()

    ok = session.poll_once()

    assert ok is False
    assert session.stats.malformed_count == 1
    assert store.get_live_capture_events(session.session_id) == []
    assert store.get_live_capture_snapshots(session.session_id) == []
    assert store.get_live_capture_session(session.session_id)["completeness"] == 0.0


def test_poll_once_treats_endpoint_unavailable_as_routine_not_fatal(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(
        store, client=FakeClient([LiveClientUnavailableError("no game running")]),
    )
    session.start()

    ok = session.poll_once()

    assert ok is False
    assert session.stats.unavailable_count == 1
    assert store.get_live_capture_events(session.session_id) == []


# ── poll_once: narrow, bounded handling of transient local storage errors ───

def test_poll_once_survives_a_transient_event_store_error_and_retries_it(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(store, client=FakeClient([ALL_GAME_DATA, ALL_GAME_DATA]))
    session.start()

    original = store.record_live_capture_events
    calls = {"n": 0}

    def flaky(session_id, events):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original(session_id, events)

    store.record_live_capture_events = flaky

    ok1 = session.poll_once()  # the events write fails
    ok2 = session.poll_once()  # store recovered -- the same batch is retried

    assert ok1 is False
    assert ok2 is True
    assert session.stats.store_errors == 1
    # Nothing was permanently lost: last_event_id was never advanced on
    # failure, so the un-persisted events were retried and landed.
    events = store.get_live_capture_events(session.session_id)
    assert len(events) == 5


def test_poll_once_survives_a_transient_snapshot_store_error(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(store, client=FakeClient([ALL_GAME_DATA]))
    session.start()

    def always_fails(session_id, snapshot_time, payload):
        raise sqlite3.OperationalError("database is locked")

    store.record_live_capture_snapshot = always_fails

    ok = session.poll_once()

    assert ok is False
    assert session.stats.store_errors == 1
    assert session.stats.snapshots_captured == 0
    # The endpoint call itself succeeded, so this is not an "unavailable".
    assert session.stats.unavailable_count == 0


def test_poll_once_survives_a_transient_progress_update_error(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(store, client=FakeClient([ALL_GAME_DATA]))
    session.start()

    def always_fails(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    store.update_live_capture_session = always_fails

    ok = session.poll_once()

    assert ok is False
    assert session.stats.store_errors == 1
    # The events/snapshot writes that happened earlier in the same poll are
    # unaffected by the later progress-update failure.
    assert len(store.get_live_capture_events(session.session_id)) == 5


def test_poll_once_does_not_catch_broad_unrelated_exceptions(tmp_path):
    """Only narrow, expected operational failures are swallowed -- a bug
    elsewhere must still surface loudly rather than being silently treated
    as a routine storage hiccup."""
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(store, client=FakeClient([ALL_GAME_DATA]))
    session.start()

    def boom(*a, **k):
        raise ValueError("not a storage problem")

    store.record_live_capture_events = boom

    with pytest.raises(ValueError):
        session.poll_once()


def test_run_loop_survives_persistent_store_errors_without_the_thread_dying(monkeypatch, tmp_path):
    monkeypatch.setattr(live_client, "UNAVAILABLE_BACKOFF_SECONDS", 0.01)
    store = HistoryStore(tmp_path / "history.db")

    def always_fails(session_id, events):
        raise sqlite3.OperationalError("disk I/O error")

    store.record_live_capture_events = always_fails
    session = LiveCaptureSession(
        store, client=FakeClient([ALL_GAME_DATA] * 50), poll_interval=0.01,
    )
    session.start()
    thread = threading.Thread(target=session.run, daemon=True)
    thread.start()
    time.sleep(0.1)
    session.request_stop()
    thread.join(timeout=2)

    # The thread must exit cleanly via request_stop(), not die silently from
    # an uncaught exception -- and it must have kept polling with backoff
    # the whole time, not given up after the first storage error.
    assert not thread.is_alive()
    assert session.stats.store_errors >= 1
    assert session.stats.polls >= 2

    status = session.finish()
    assert status == "partial_storage_errors"  # evidence was dropped


def test_finish_infers_completed_when_data_was_captured(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(store, client=FakeClient([ALL_GAME_DATA]))
    session.start()
    session.poll_once()

    status = session.finish()

    assert status == "completed"
    assert store.get_live_capture_session(session.session_id)["status"] == "completed"


def test_finish_infers_partial_no_data_when_nothing_was_ever_captured(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(
        store, client=FakeClient([LiveClientUnavailableError("down")]),
    )
    session.start()
    session.poll_once()

    status = session.finish()

    assert status == "partial_no_data"


def test_finish_infers_partial_storage_errors_when_capture_is_empty_because_of_them(tmp_path):
    """An empty capture caused by every store write failing must be
    reported as partial_storage_errors, not the generic partial_no_data --
    the latter would hide the authoritative reason nothing landed."""
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(store, client=FakeClient([ALL_GAME_DATA]))
    session.start()

    def boom_events(session_id, events):
        raise sqlite3.OperationalError("database is locked")

    def boom_snapshot(session_id, snapshot_time, payload):
        raise sqlite3.OperationalError("database is locked")

    def boom_progress(session_id, last_event_id=None, completeness=None):
        raise sqlite3.OperationalError("database is locked")

    store.record_live_capture_events = boom_events
    store.record_live_capture_snapshot = boom_snapshot
    store.update_live_capture_session = boom_progress

    ok = session.poll_once()

    assert ok is False
    assert session.stats.events_captured == 0
    assert session.stats.snapshots_captured == 0
    assert session.stats.store_errors > 0

    status = session.finish()

    assert status == "partial_storage_errors"


def test_finish_infers_partial_endpoint_lost_after_prolonged_outage(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(store, client=FakeClient([ALL_GAME_DATA]))
    session.start()
    session.poll_once()
    session.stats.consecutive_unavailable = MAX_UNAVAILABLE_BEFORE_PARTIAL

    status = session.finish()

    assert status == "partial_endpoint_lost"


def test_finish_status_can_be_overridden_explicitly(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(store, client=FakeClient([ALL_GAME_DATA]))
    session.start()

    status = session.finish(status="partial_client_closed")

    assert status == "partial_client_closed"


def test_session_reconcile_delegates_to_store(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(store, client=FakeClient([ALL_GAME_DATA]))
    session.start()

    assert session.reconcile(4242) == "reconciled"
    assert store.get_live_capture_session(session.session_id)["game_id"] == 4242


def test_run_loop_ends_the_session_after_prolonged_unavailability(monkeypatch, tmp_path):
    monkeypatch.setattr(live_client, "MAX_UNAVAILABLE_BEFORE_PARTIAL", 3)
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(
        store, client=FakeClient([LiveClientUnavailableError("down")] * 10),
        poll_interval=0.01,
    )
    session.start()
    monkeypatch.setattr(live_client, "UNAVAILABLE_BACKOFF_SECONDS", 0.01)

    session.run()

    assert session.stats.consecutive_unavailable >= 3
    assert session.stats.polls >= 3


def test_run_loop_stops_promptly_on_request(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(
        store, client=FakeClient([ALL_GAME_DATA]), poll_interval=0.01,
    )
    session.start()
    thread = threading.Thread(target=session.run, daemon=True)
    thread.start()
    time.sleep(0.05)
    session.request_stop()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert session.stats.polls >= 1


# ── LiveCaptureManager: lifecycle, crash recovery, reconnect, reconciliation ─

def test_manager_start_stop_persists_a_completed_session(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    manager = LiveCaptureManager(
        store, client_factory=lambda: FakeClient([ALL_GAME_DATA]),
        session_factory=lambda store, client, on_log: LiveCaptureSession(
            store, client, on_log=on_log, poll_interval=0.01,
        ),
    )

    session_id = manager.start(expected_game_id=None)
    time.sleep(0.05)
    stopped_id, status = manager.stop()

    assert session_id is not None
    assert stopped_id == session_id
    assert status == "completed"
    assert store.get_live_capture_session(session_id)["status"] == "completed"


def test_manager_stop_on_no_active_session_is_a_safe_no_op(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    manager = LiveCaptureManager(store)

    assert manager.stop() == (None, None)


def test_manager_stop_retains_a_session_whose_finish_fails_and_retries_it(tmp_path):
    """If finish() fails to persist the terminal status, the session must
    not be forgotten -- it should be retried, and it must not become
    eligible for a brand new game to resume/mix into."""
    store = HistoryStore(tmp_path / "history.db")
    manager = LiveCaptureManager(
        store, client_factory=lambda: FakeClient([ALL_GAME_DATA]),
        session_factory=lambda store, client, on_log: LiveCaptureSession(
            store, client, on_log=on_log, poll_interval=0.01,
        ),
        on_log=lambda *a: None,
    )
    session_id = manager.start(expected_game_id=None)
    time.sleep(0.05)

    original_finalize = store.finalize_live_capture_session

    def flaky_finalize(session_id, status, ended_at=None):
        raise sqlite3.OperationalError("database is locked")

    store.finalize_live_capture_session = flaky_finalize

    stopped_id, status = manager.stop()

    assert stopped_id == session_id
    assert status is None  # finish() failed -- no terminal status yet
    assert store.get_live_capture_session(session_id)["status"] == "active"

    # A brand new game must NOT resume/mix with the still-unfinalized orphan.
    new_session_id = manager.start(expected_game_id=999)
    assert new_session_id != session_id
    # The orphan must also not be swept/relabeled by the stale-session sweep
    # while its own finalize is still pending retry.
    assert store.get_live_capture_session(session_id)["status"] == "active"

    time.sleep(0.05)
    store.finalize_live_capture_session = original_finalize  # repair the store
    new_stopped_id, new_status = manager.stop()

    assert new_stopped_id == new_session_id
    assert new_status == "completed"
    # The orphan's finalize was retried (and succeeded) as part of the next
    # start(), using its originally-requested status (None -> inferred).
    assert store.get_live_capture_session(session_id)["status"] == "completed"


def test_manager_stop_reports_resolved_status_when_its_own_immediate_retry_succeeds(tmp_path):
    """finish() fails once (a one-shot transient DB error), but stop()'s own
    opportunistic _retry_pending_finalizes() pass -- run immediately after,
    in the same stop() call -- succeeds. stop() must report that resolved
    terminal status, not a stale (session_id, None) while the DB row is
    already terminal."""
    store = HistoryStore(tmp_path / "history.db")
    manager = LiveCaptureManager(
        store, client_factory=lambda: FakeClient([ALL_GAME_DATA]),
        session_factory=lambda store, client, on_log: LiveCaptureSession(
            store, client, on_log=on_log, poll_interval=0.01,
        ),
        on_log=lambda *a: None,
    )
    session_id = manager.start(expected_game_id=None)
    time.sleep(0.05)

    original_finalize = store.finalize_live_capture_session
    calls = {"n": 0}

    def one_shot_flaky_finalize(session_id, status, ended_at=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_finalize(session_id, status, ended_at=ended_at)

    store.finalize_live_capture_session = one_shot_flaky_finalize

    stopped_id, status = manager.stop()

    assert stopped_id == session_id
    assert calls["n"] >= 2  # the initial failure, then the immediate retry
    # The immediate retry inside this same stop() call already landed the
    # terminal status -- reporting None here would contradict the DB.
    assert status == "completed"
    assert store.get_live_capture_session(session_id)["status"] == "completed"


def test_manager_recovers_a_crashed_session_left_active(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.start_live_capture_session("crashed-session")  # simulates a prior process dying

    manager = LiveCaptureManager(store, on_log=lambda *a: None)
    recovered = manager.recover_stale_sessions()

    assert recovered == 1
    assert store.get_live_capture_session("crashed-session")["status"] == "partial_process_restart"


def test_manager_recover_stale_sessions_excludes_locally_pending_finalizes(tmp_path):
    """A session queued for local finalize-retry is not crash residue from
    a *previous* process -- recover_stale_sessions() must leave it alone,
    exactly like start()'s stale sweep does, so a recover pass can't
    relabel a locally retryable finalize as partial_process_restart."""
    store = HistoryStore(tmp_path / "history.db")
    manager = LiveCaptureManager(
        store, client_factory=lambda: FakeClient([ALL_GAME_DATA]),
        session_factory=lambda store, client, on_log: LiveCaptureSession(
            store, client, on_log=on_log, poll_interval=0.01,
        ),
        on_log=lambda *a: None,
    )
    session_id = manager.start(expected_game_id=None)
    time.sleep(0.05)

    original_finalize = store.finalize_live_capture_session

    def flaky_finalize(session_id, status, ended_at=None):
        raise sqlite3.OperationalError("database is locked")

    store.finalize_live_capture_session = flaky_finalize
    stopped_id, status = manager.stop()

    assert status is None  # finish() failed -- queued in _pending_finalize
    assert store.get_live_capture_session(session_id)["status"] == "active"

    store.finalize_live_capture_session = original_finalize  # repair the store
    store.start_live_capture_session("crashed-elsewhere")  # unrelated crash residue

    recovered = manager.recover_stale_sessions()

    assert recovered == 1
    assert store.get_live_capture_session("crashed-elsewhere")["status"] == "partial_process_restart"
    # Still 'active': it's being retried locally, not crash residue from a
    # previous process -- must not be relabeled out from under the retry.
    assert store.get_live_capture_session(session_id)["status"] == "active"


def test_manager_start_resumes_a_matching_stale_session_instead_of_a_new_one(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.start_live_capture_session("crashed-mid-game", game_id=None)
    store.record_live_capture_events("crashed-mid-game", [
        {"event_id": 0, "event_time": 0.0, "event_type": "GameStart", "payload": {}},
    ])
    store.start_live_capture_session("unrelated-stale-session", game_id=42)

    manager = LiveCaptureManager(
        store, client_factory=lambda: FakeClient([ALL_GAME_DATA]),
        # A large poll_interval means the background thread's run() loop
        # executes exactly one poll_once() during the sleep below, keeping
        # the assertions below deterministic.
        session_factory=lambda store, client, on_log: LiveCaptureSession(
            store, client, on_log=on_log, poll_interval=1000.0,
        ),
    )

    session_id = manager.start(expected_game_id=987)
    time.sleep(0.05)
    manager.stop()

    assert session_id == "crashed-mid-game"  # resumed, not replaced
    # last_event_id carried over from the crashed session, so the resumed
    # poll only adds the genuinely new events (1-4); event 0 is not
    # re-persisted or double-counted.
    events = store.get_live_capture_events("crashed-mid-game")
    assert [e["event_id"] for e in events] == [0, 1, 2, 3, 4]
    # The unrelated stale session gets swept as crash residue once we know
    # which session legitimately continues.
    assert store.get_live_capture_session("unrelated-stale-session")["status"] == \
        "partial_process_restart"


def test_manager_reconcile_attaches_the_authoritative_game_id(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    manager = LiveCaptureManager(
        store, client_factory=lambda: FakeClient([ALL_GAME_DATA]),
        session_factory=lambda store, client, on_log: LiveCaptureSession(
            store, client, on_log=on_log, poll_interval=0.01,
        ),
    )
    session_id = manager.start(expected_game_id=None)
    manager.stop()

    result = manager.reconcile(654321, session_id)

    assert result == "reconciled"
    assert store.get_live_capture_session(session_id)["game_id"] == 654321


def test_manager_reconcile_flags_a_mismatch_instead_of_overwriting(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    manager = LiveCaptureManager(
        store, client_factory=lambda: FakeClient([ALL_GAME_DATA]),
        session_factory=lambda store, client, on_log: LiveCaptureSession(
            store, client, on_log=on_log, poll_interval=0.01,
        ),
    )
    session_id = manager.start(expected_game_id=111)
    manager.stop()

    manager.reconcile(111, session_id)  # matches what we expected -- no-op reconciled
    result = manager.reconcile(222, session_id)  # a different, later-known ID -- mismatch

    assert result == "mismatch"
    session = store.get_live_capture_session(session_id)
    assert session["game_id"] == 111
    assert session["status"] == "reconciliation_mismatch"


def test_manager_reconcile_is_a_no_op_when_game_id_is_unresolved(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    manager = LiveCaptureManager(store)

    assert manager.reconcile(None, "some-session") is None


def test_manager_reconcile_is_a_no_op_when_session_id_is_unknown(tmp_path):
    """A resolved game_id with no known session_id (e.g. postgame ingestion
    resolved after a full app restart, so no capture ran this process) must
    never fall back to guessing "the latest" session -- it is a no-op."""
    store = HistoryStore(tmp_path / "history.db")
    manager = LiveCaptureManager(
        store, client_factory=lambda: FakeClient([ALL_GAME_DATA]),
        session_factory=lambda store, client, on_log: LiveCaptureSession(
            store, client, on_log=on_log, poll_interval=0.01,
        ),
    )
    unrelated_session_id = manager.start(expected_game_id=None)
    manager.stop()

    assert manager.reconcile(999) is None
    assert store.get_live_capture_session(unrelated_session_id)["game_id"] is None


def test_manager_reconcile_targets_the_exact_stopped_session_despite_a_newer_game(tmp_path):
    """Regression test for the async postgame-ingestion race: game A ends
    and its session is stopped (yielding its session_id); before game A's
    slow post-game ingestion resolves the authoritative game_id, game B
    starts and gets its own new session. Reconciling game A's ID must land
    on game A's session, never on game B's newer one."""
    store = HistoryStore(tmp_path / "history.db")
    manager = LiveCaptureManager(
        store, client_factory=lambda: FakeClient([ALL_GAME_DATA]),
        session_factory=lambda store, client, on_log: LiveCaptureSession(
            store, client, on_log=on_log, poll_interval=0.01,
        ),
    )

    session_a_id = manager.start(expected_game_id=None)
    time.sleep(0.05)
    stopped_a_id, _ = manager.stop()
    assert stopped_a_id == session_a_id

    # Game B starts (its own new session) before game A's async postgame
    # ingestion has resolved game A's authoritative ID.
    session_b_id = manager.start(expected_game_id=None)
    assert session_b_id != session_a_id

    # Game A's ingestion now resolves late and reconciles using the
    # session_id captured at stop() time, not "the latest" session.
    result = manager.reconcile(111111, session_a_id)
    manager.stop()

    assert result == "reconciled"
    assert store.get_live_capture_session(session_a_id)["game_id"] == 111111
    assert store.get_live_capture_session(session_b_id)["game_id"] is None


def test_manager_reconcile_of_an_unresolved_remake_never_attaches_to_the_next_game(tmp_path):
    """game_id=None (remake/unsupported queue) must leave the session's
    game_id permanently unattached -- and, since reconciliation only ever
    targets an explicit session_id, it can never later be mistaken for a
    completely different game's session."""
    store = HistoryStore(tmp_path / "history.db")
    manager = LiveCaptureManager(
        store, client_factory=lambda: FakeClient([ALL_GAME_DATA]),
        session_factory=lambda store, client, on_log: LiveCaptureSession(
            store, client, on_log=on_log, poll_interval=0.01,
        ),
    )

    remake_session_id = manager.start(expected_game_id=None)
    time.sleep(0.05)
    manager.stop()
    assert manager.reconcile(None, remake_session_id) is None  # remake: no game_id ever resolved

    next_game_session_id = manager.start(expected_game_id=None)
    time.sleep(0.05)
    manager.stop()
    manager.reconcile(222222, next_game_session_id)

    assert store.get_live_capture_session(remake_session_id)["game_id"] is None
    assert store.get_live_capture_session(next_game_session_id)["game_id"] == 222222


def test_manager_start_is_idempotent_while_already_capturing(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    manager = LiveCaptureManager(
        store, client_factory=lambda: FakeClient([ALL_GAME_DATA]),
        session_factory=lambda store, client, on_log: LiveCaptureSession(
            store, client, on_log=on_log, poll_interval=0.01,
        ),
    )

    first = manager.start(expected_game_id=1)
    second = manager.start(expected_game_id=2)  # game already in flight -- ignored
    manager.stop()

    assert first == second
    assert len(store.list_live_capture_sessions()) == 1


def test_manager_never_crashes_when_endpoint_is_unavailable_the_whole_game(tmp_path):
    """Preserves existing RuneSync behavior: no game / no endpoint must not
    raise or produce a success-shaped empty capture."""
    store = HistoryStore(tmp_path / "history.db")
    manager = LiveCaptureManager(
        store,
        client_factory=lambda: FakeClient([LiveClientUnavailableError("no game")] * 20),
        session_factory=lambda store, client, on_log: LiveCaptureSession(
            store, client, on_log=on_log, poll_interval=0.01,
        ),
        on_log=lambda *a: None,
    )

    session_id = manager.start(expected_game_id=None)
    time.sleep(0.05)
    _, status = manager.stop()

    assert status == "partial_no_data"
    assert store.get_live_capture_events(session_id) == []
    assert store.get_live_capture_snapshots(session_id) == []


# ── profiling: representative fixture size and CPU cost -> bounded defaults ─

def test_storage_budget_bounded_for_a_full_game(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(store, client=FakeClient([ALL_GAME_DATA]))
    session.start()
    snapshot = normalize_snapshot(ALL_GAME_DATA)
    compressed_size = len(zlib.compress(json.dumps(snapshot).encode("utf-8"), level=9))

    # A representative snapshot should compress well below the raw fixture
    # size, and a ~45 minute game (2700s) at the module's snapshot cadence
    # produces a bounded number of stored snapshots.
    raw_size = len((FIXTURES / "live_client_all_game_data.json").read_bytes())
    assert compressed_size < raw_size
    expected_snapshot_count = int(2700 // SNAPSHOT_INTERVAL_SECONDS) + 1
    estimated_total_bytes = expected_snapshot_count * compressed_size
    assert expected_snapshot_count <= 200
    assert estimated_total_bytes < 1_000_000  # under 1 MB per full game


def test_poll_cpu_cost_is_negligible(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    session = LiveCaptureSession(
        store, client=FakeClient([ALL_GAME_DATA] * 250), poll_interval=0.0,
        clock=lambda: 0.0,  # freeze the clock: never re-triggers a snapshot after the first
    )
    session.start()

    start = time.perf_counter()
    for _ in range(200):
        session.poll_once()
    elapsed = time.perf_counter() - start

    # 200 in-process polls (no real network I/O) should be sub-second; the
    # real cadence is sleep-bound (POLL_INTERVAL_SECONDS), not CPU-bound.
    assert elapsed < 1.0
