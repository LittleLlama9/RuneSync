from unittest.mock import MagicMock

import bridge
from bridge import Api


def _api():
    api = Api.__new__(Api)
    api.history = MagicMock()
    api.live_capture = MagicMock()
    api.live_capture.stop.return_value = (None, None)
    api.pusher = MagicMock()
    api.snap = {"inGame": False}
    api._emit = MagicMock()
    return api


def test_interface_style_defaults_and_persists(monkeypatch):
    api = _api()
    api.overrides = MagicMock()
    api.overrides.settings = {}
    api.overrides.save_settings = MagicMock()
    api.monitor = None
    monkeypatch.setattr(bridge, "is_autostart_enabled", lambda: False)

    assert api._settings()["interface_style"] == "standard"
    assert api.set_interface("classic") == {
        "ok": True, "interface_style": "classic",
    }
    api.overrides.save_settings.assert_called_once_with(
        {"interface_style": "classic"},
    )
    assert api.set_interface("invalid") == {
        "ok": False, "error": "invalid interface style",
    }
    api.overrides.settings = {"server_url": "https://example.invalid"}
    api.overrides.save_settings.reset_mock()
    assert api.save_settings({"interface_style": "classic"}) == {"ok": True}
    api.overrides.save_settings.assert_called_once_with({
        "server_url": "https://example.invalid",
        "interface_style": "classic",
    })
    api.overrides.settings = {"interface_style": "retro"}
    assert api._settings()["interface_style"] == "standard"
    api.overrides.save_settings.reset_mock()
    assert api.save_settings({"interface_style": "bogus"}) == {"ok": True}
    api.overrides.save_settings.assert_called_once_with({
        "interface_style": "standard",
    })


def test_history_api_delegates_to_service():
    api = _api()
    api.history.summary.return_value = {"overall": {"games": 2}}
    api.history.list_history.return_value = [{"game_id": 123}]
    api.history.report.return_value = {"match": {"game_id": 123}}

    assert api.get_history_summary()["overall"]["games"] == 2
    assert api.get_match_history(5, 10) == [{"game_id": 123}]
    assert api.get_match_report(123)["match"]["game_id"] == 123

    api.history.list_history.assert_called_once_with(5, 10)
    api.history.report.assert_called_once_with(123)


def test_builds_include_spell_ids_for_icon_rendering():
    api = _api()
    api.overrides = MagicMock()
    api.overrides.all.return_value = {
        "Azir": {
            "role": "auto", "primary_tree": "Precision",
            "secondary_tree": "Sorcery", "spell1": 4, "spell2": 12,
        },
    }

    assert api.get_builds() == [{
        "champ": "Azir", "role": "auto", "path": "Precision × Sorcery",
        "summoners": "FLASH / TELEPORT", "spell1": 4, "spell2": 12,
    }]


def test_game_lifecycle_captures_and_imports(monkeypatch):
    api = _api()
    api.live_capture.stop.return_value = ("session-123", "completed")

    class ImmediateThread:
        def __init__(self, target, daemon=True, args=()):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(bridge.threading, "Thread", ImmediateThread)
    api._ingest_postgame = MagicMock()

    api._on_game(True)
    api.history.capture_active_game.assert_called_once()
    api.live_capture.start.assert_called_once_with(api.history.active_game_id)
    api._on_game(False)
    api._ingest_postgame.assert_called_once_with("session-123")
    api.live_capture.stop.assert_called_once_with(status=None)


def test_game_end_without_postgame_import_marks_live_capture_partial():
    api = _api()
    api._ingest_postgame = MagicMock()

    api._on_game(False, import_postgame=False)

    api.live_capture.stop.assert_called_once_with(status="partial_client_closed")
    api._ingest_postgame.assert_not_called()


def test_postgame_ready_pushes_event_and_shows_window(monkeypatch):
    api = _api()
    window = MagicMock()
    monkeypatch.setattr(Api, "_win", staticmethod(lambda: window))

    api._on_postgame_ready(123)

    api.pusher.push.assert_called_once_with(
        "postgame_ready", {"game_id": 123},
    )
    window.show.assert_called_once()


def test_reconnect_only_recovers_postgame_in_terminal_phase():
    api = _api()
    api._pending_postgame_recovery = True
    api._ingest_postgame = MagicMock()
    api._sync_history = MagicMock()
    api.lcu = MagicMock()

    api.lcu.get_game_flow_phase.return_value = "Reconnect"
    api._recover_postgame_after_reconnect()
    api._ingest_postgame.assert_not_called()
    api._sync_history.assert_called_once()
    api.history.capture_active_game.assert_called_once()
    api.live_capture.start.assert_called_once_with(api.history.active_game_id)

    api._pending_postgame_recovery = True
    api._sync_history.reset_mock()
    api.live_capture.start.reset_mock()
    api.lcu.get_game_flow_phase.return_value = "EndOfGame"
    api._recover_postgame_after_reconnect()
    api._ingest_postgame.assert_called_once()
    api._sync_history.assert_called_once()
    api.live_capture.start.assert_not_called()


def test_ingest_postgame_reconciles_live_capture_with_resolved_game_id():
    api = _api()
    api.history.ingest_after_game.return_value = 555

    api._ingest_postgame(live_capture_session_id="session-abc")

    api.live_capture.reconcile.assert_called_once_with(555, "session-abc")


def test_ingest_postgame_skips_reconcile_when_game_id_unresolved():
    api = _api()
    api.history.ingest_after_game.return_value = None

    api._ingest_postgame(live_capture_session_id="session-abc")

    api.live_capture.reconcile.assert_not_called()


def test_ingest_postgame_skips_reconcile_when_no_session_id_is_known():
    """Reconnect-after-full-restart path: postgame ingestion can resolve a
    game_id even though this process never started/stopped a live capture
    session for it. Reconciliation must never guess at "the latest"
    session in that case -- it is simply skipped."""
    api = _api()
    api.history.ingest_after_game.return_value = 555

    api._ingest_postgame()  # no live_capture_session_id supplied

    api.live_capture.reconcile.assert_not_called()


def test_stale_live_capture_recovered_only_outside_active_game_phase():
    api = _api()
    api.lcu = MagicMock()

    api.lcu.get_game_flow_phase.return_value = "ChampSelect"
    api._recover_stale_live_capture()
    api.live_capture.recover_stale_sessions.assert_called_once()

    api.live_capture.recover_stale_sessions.reset_mock()
    api.lcu.get_game_flow_phase.return_value = "InProgress"
    api._recover_stale_live_capture()
    api.live_capture.recover_stale_sessions.assert_not_called()
