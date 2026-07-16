from unittest.mock import MagicMock

import bridge
from bridge import Api


def _api():
    api = Api.__new__(Api)
    api.history = MagicMock()
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


def test_game_lifecycle_captures_and_imports(monkeypatch):
    api = _api()

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(bridge.threading, "Thread", ImmediateThread)
    api._ingest_postgame = MagicMock()

    api._on_game(True)
    api.history.capture_active_game.assert_called_once()
    api._on_game(False)
    api._ingest_postgame.assert_called_once()


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

    api._pending_postgame_recovery = True
    api._sync_history.reset_mock()
    api.lcu.get_game_flow_phase.return_value = "EndOfGame"
    api._recover_postgame_after_reconnect()
    api._ingest_postgame.assert_called_once()
    api._sync_history.assert_called_once()
