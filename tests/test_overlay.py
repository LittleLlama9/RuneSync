"""Tests for the champ-select overlay: the structured counters push, the anchor
geometry helper (multi-monitor aware), and the Pillow panel renderer."""
from unittest.mock import MagicMock

import bridge
import overlay


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


# ── overlay anchor geometry ──────────────────────────────────────────────────
def test_anchor_docks_top_right_inside_client():
    # Client (100,100)-(1380,820); a 300-wide panel anchors just inside the
    # client's right edge, below its top bar.
    x, y = overlay.overlay_anchor((100, 100, 1380, 820), 300, 400,
                                  0, 0, 1920, 1080)
    assert x == 1380 - 300 - overlay._MARGIN
    assert y == 100 + overlay._TOP_OFFSET


def test_anchor_clamps_to_primary_screen():
    # A panel that would run off the bottom/right is pulled back on screen.
    x, y = overlay.overlay_anchor((0, 900, 1920, 1080), 300, 400,
                                  0, 0, 1920, 1080)
    assert x + 300 <= 1920
    assert y + 400 <= 1080


def test_anchor_negative_origin_monitor_left_of_primary():
    # Client on a monitor to the LEFT of the primary (negative x origin). The
    # panel must stay on that monitor, not be yanked back to x>=0.
    x, y = overlay.overlay_anchor((-1820, 100, -540, 820), 300, 400,
                                  -1920, 0, 1920, 1080)
    assert x == -540 - 300 - overlay._MARGIN
    assert -1920 <= x and x + 300 <= 0


def test_anchor_negative_origin_monitor_above_primary():
    # Client on a monitor ABOVE the primary (negative y origin). y must not be
    # clamped up to 0 (which would detach the panel onto the primary monitor).
    x, y = overlay.overlay_anchor((100, -980, 1380, -260), 300, 400,
                                  0, -1080, 1920, 1080)
    assert y == -980 + overlay._TOP_OFFSET
    assert y >= -1080


def test_find_client_window_safe_without_win32(monkeypatch):
    # When win32 is unavailable the finder must degrade to None, never raise.
    monkeypatch.setattr(overlay, "_HAVE_WIN32", False)
    assert overlay.find_client_window() is None


# ── panel renderer ───────────────────────────────────────────────────────────
def test_render_panel_none_when_no_state():
    assert overlay.render_panel(None) is None
    assert overlay.render_panel({}) is None


def test_render_panel_header_only_when_no_data():
    # In champ select but no data yet: still renders (a live header), not None.
    img = overlay.render_panel({"running": True}, "amber")
    assert img is not None
    assert img.size[0] == overlay.PANEL_WIDTH
    assert img.mode == "RGBA"


def test_render_panel_with_matchup_and_counters():
    state = {
        "running": True, "champ": "Sion", "enemy": "Darius",
        "wr": 48.5, "wrLabel": "LOSING", "wrTag": "warn",
        "counters": {"active": True, "enemy": "Darius",
                     "counters": [{"champion": "Quinn", "win_rate": 54.2},
                                  {"champion": "Vayne", "win_rate": 52.0}]},
        "draft": {"observations": [{"level": "info", "text": "Enemy is AD-heavy"}]},
        "theme": "amber",
    }
    img = overlay.render_panel(state)
    assert img is not None
    # With three populated sections the panel is meaningfully taller than a
    # header-only render.
    header_only = overlay.render_panel({"running": True})
    assert img.size[1] > header_only.size[1]


def test_render_panel_escapes_long_names_without_error():
    # Absurdly long champion/enemy names must render (truncated) not crash.
    state = {"running": True, "champ": "X" * 200, "enemy": "Y" * 200,
             "wr": 50.0, "wrLabel": "EVEN", "wrTag": "info"}
    assert overlay.render_panel(state) is not None


def test_render_panel_draft_picks_render_without_error():
    # Observations carrying a `picks` list draw a second (gold) champion line.
    state = {"running": True, "draft": {"observations": [
        {"level": "warn", "short": "No hard engage",
         "text": "Your team has no reliable hard engage.",
         "picks": ["Leona", "Nautilus", "Rell"]},
        {"level": "warn", "short": "All AD, add magic dmg",
         "text": "Your team is fully AD.", "picks": ["Sylas"]},
    ]}}
    img = overlay.render_panel(state)
    assert img is not None
    # Picks add a line, so this is taller than the same obs without picks.
    no_picks = overlay.render_panel({"running": True, "draft": {"observations": [
        {"level": "warn", "short": "No hard engage", "text": "x"},
        {"level": "warn", "short": "All AD, add magic dmg", "text": "y"},
    ]}})
    assert img.size[1] > no_picks.size[1]


def test_premultiplied_bgra_order_and_size():
    # A 1x1 opaque red RGBA pixel -> premultiplied BGRA bytes = B,G,R,A.
    from PIL import Image
    px = Image.new("RGBA", (1, 1), (255, 0, 0, 255))
    buf = overlay._to_premultiplied_bgra(px)
    assert len(buf) == 4
    assert tuple(buf) == (0, 0, 255, 255)   # B=0, G=0, R=255, A=255

    # Half-transparent green: channel is premultiplied by alpha (128*128//255).
    px = Image.new("RGBA", (1, 1), (0, 255, 0, 128))
    b, g, r, a = tuple(overlay._to_premultiplied_bgra(px))
    assert a == 128 and r == 0 and b == 0
    assert 120 <= g <= 130                    # 255*128/255 ≈ 128, premultiplied
