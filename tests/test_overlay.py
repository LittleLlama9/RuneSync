"""Tests for the champ-select overlay: Pusher mirroring, the structured counters
push, and the anchor geometry helper."""
from unittest.mock import MagicMock

import bridge
import overlay


# ── Pusher mirror ────────────────────────────────────────────────────────────
def test_mirror_forwards_only_whitelisted_events():
    main = bridge.Pusher()
    ov = bridge.Pusher()
    main.add_mirror(ov, {"matchup", "counters"})

    main.push("matchup", {"wr": 53})
    main.push("log", {"msg": "hi"})
    main.push("counters", {"active": True})

    # Main window sees everything.
    main_events = [e["event"] for e in main.drain()]
    assert main_events == ["matchup", "log", "counters"]
    # Overlay sees only the whitelisted subset, in order.
    ov_events = [e["event"] for e in ov.drain()]
    assert ov_events == ["matchup", "counters"]


def test_mirror_none_forwards_everything():
    main = bridge.Pusher()
    ov = bridge.Pusher()
    main.add_mirror(ov)
    main.push("a"); main.push("b")
    assert [e["event"] for e in ov.drain()] == ["a", "b"]


def test_drain_is_independent_per_queue():
    # Draining the overlay queue must not empty the main queue (both are
    # independent pull consumers).
    main = bridge.Pusher()
    ov = bridge.Pusher()
    main.add_mirror(ov, {"matchup"})
    main.push("matchup", {})
    ov.drain()
    assert len(main.drain()) == 1


# ── structured counters push ─────────────────────────────────────────────────
def _counters_api():
    api = bridge.Api.__new__(bridge.Api)
    api.pusher = bridge.Pusher()
    api.overrides = MagicMock()
    api.overrides.settings = {"rank": "Diamond+", "region": "NA"}
    api.snap = bridge.Api._idle_snapshot()
    return api


def test_on_counters_builds_payload_and_pushes():
    api = _counters_api()
    api._on_counters("Darius", "top", [
        {"champion": "Quinn", "win_rate": 54.2, "games": 1200},
        {"champion": "Vayne", "win_rate": 52.0, "games": 800},
    ])
    assert api.snap["counters"]["active"] is True
    assert api.snap["counters"]["enemy"] == "Darius"
    assert [c["champion"] for c in api.snap["counters"]["counters"]] == ["Quinn", "Vayne"]

    evts = api.pusher.drain()
    assert len(evts) == 1
    p = evts[0]["payload"]
    assert evts[0]["event"] == "counters"
    assert p["role"] == "top"
    assert p["sample"] == "DIAMOND+ · NA"
    assert p["counters"][0] == {"champion": "Quinn", "win_rate": 54.2, "games": 1200}


def test_on_counters_empty_ships_inactive():
    api = _counters_api()
    api._on_counters("Teemo", "top", [])
    assert api.snap["counters"] is None
    p = api.pusher.drain()[0]["payload"]
    assert p["active"] is False and p["counters"] == []


def test_on_counters_skips_rows_without_champion():
    api = _counters_api()
    api._on_counters("Teemo", "top", [
        {"champion": "", "win_rate": 51.0},
        {"win_rate": 50.0},
        {"champion": "Malphite", "win_rate": 55.5, "games": 900},
    ])
    rows = api.snap["counters"]["counters"]
    assert [c["champion"] for c in rows] == ["Malphite"]


# ── overlay geometry ─────────────────────────────────────────────────────────
def test_geometry_docks_to_right_when_room():
    # 1280x720 client at (100,100)-(1380,820) on a 1920x1080 screen: room to
    # the right, so dock the panel just past the client's right edge.
    x, y, w, h = overlay.overlay_geometry((100, 100, 1380, 820), 1920, 1080, panel_w=300)
    assert x == 1380
    assert y == 100
    assert w == 300
    assert h == 720


def test_geometry_docks_inside_when_no_room_right():
    # Client hugs the right screen edge: no room outside, so tuck against the
    # client's inner-right edge (overlapping) and stay on screen.
    x, y, w, h = overlay.overlay_geometry((640, 100, 1920, 820), 1920, 1080, panel_w=300)
    assert x == 1620          # 1920 - 300
    assert x + w <= 1920


def test_geometry_clamps_height_and_bottom_to_screen():
    # A tall client that would run the panel off the bottom gets clamped.
    x, y, w, h = overlay.overlay_geometry((0, 0, 1280, 1200), 1920, 1080, panel_w=300)
    assert h <= 1080
    assert y + h <= 1080


def test_geometry_enforces_minimum_height():
    x, y, w, h = overlay.overlay_geometry((100, 100, 400, 180), 1920, 1080, panel_w=300)
    assert h >= 240


def test_geometry_negative_origin_monitor_left_of_primary():
    # Client on a monitor to the LEFT of the primary (negative x origin). The
    # panel must dock relative to that monitor, not be yanked back to x=0.
    x, y, w, h = overlay.overlay_geometry(
        (-1820, 100, -540, 820), 1920, 1080, panel_w=300,
        screen_x=-1920, screen_y=0)
    assert x == -540          # just right of the client, still negative
    assert y == 100
    assert x >= -1920 and x + w <= 0


def test_geometry_negative_origin_monitor_above_primary():
    # Client on a monitor ABOVE the primary (negative y origin). y must not be
    # clamped up to 0 (which would detach the panel onto the primary monitor).
    x, y, w, h = overlay.overlay_geometry(
        (100, -980, 1380, -260), 1920, 1080, panel_w=300,
        screen_x=0, screen_y=-1080)
    assert y == -980
    assert y >= -1080


def test_find_client_window_safe_without_win32(monkeypatch):
    # When win32 is unavailable the finder must degrade to None, never raise.
    monkeypatch.setattr(overlay, "_HAVE_WIN32", False)
    assert overlay.find_client_window() is None
