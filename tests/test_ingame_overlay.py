"""Tests for the in-game overlay: the pure state gates (skill point, gold
formatting, Tab read), the indicator compositor, the screen-region shop detector
(with an injected framebuffer grab), and the controller change-signature.

Rendering is validated structurally (which indicators appear at which size) so
these run headless with no game window; the GDI/anchor plumbing is exercised at
runtime like overlay.py."""
from PIL import Image

import ingame_overlay as igo
import bridge


# ── pure state gates ─────────────────────────────────────────────────────────
def test_skill_point_available_true_when_level_exceeds_spent():
    hud = {"me": {"level": 6},
           "skill": {"next": "R", "ranks": {"Q": 3, "W": 1, "E": 1, "R": 0}}}
    assert igo.skill_point_available(hud) is True   # 6 - 5 = 1 unspent


def test_skill_point_available_false_when_all_spent():
    hud = {"me": {"level": 5},
           "skill": {"next": "W", "ranks": {"Q": 3, "W": 1, "E": 1, "R": 0}}}
    assert igo.skill_point_available(hud) is False  # 5 - 5 = 0


def test_skill_point_available_false_when_maxed_or_missing():
    assert igo.skill_point_available(None) is False
    assert igo.skill_point_available({"me": {"level": 18}, "skill": {"next": None}}) is False
    assert igo.skill_point_available({"me": {"level": 3}}) is False


def test_fmt_gold_compact_and_signed():
    assert igo.fmt_gold(2200) == "+2.2k"
    assert igo.fmt_gold(-850) == "-850"
    assert igo.fmt_gold(0) == "+0"
    assert igo.fmt_gold(999) == "+999"
    assert igo.fmt_gold(-1500) == "-1.5k"


def test_read_tab_held_uses_injected_key_state():
    assert igo.read_tab_held(get_key=lambda vk: 0x8000) is True
    assert igo.read_tab_held(get_key=lambda vk: 0x0001) is False   # toggled, not down
    assert igo.read_tab_held(get_key=lambda vk: 0) is False


# ── indicator compositor ─────────────────────────────────────────────────────
_HUD = {"me": {"level": 6},
        "team_gold": {"ours": 24300, "theirs": 22100, "diff": 2200},
        "skill": {"next": "R", "ranks": {"Q": 3, "W": 1, "E": 1, "R": 0}}}
_RECS = {"suggestions": [{"reason": "vs AP burst",
                          "items": [{"name": "Maw of Malmortius"}]}],
         "notes": ["Enemy is AP-heavy"]}


def test_compose_none_when_no_indicator_active():
    # No Tab, shop closed, and no skill point => nothing to draw.
    hud = {"me": {"level": 5},
           "skill": {"next": "W", "ranks": {"Q": 3, "W": 1, "E": 1, "R": 0}},
           "team_gold": {"diff": 100}}
    assert igo.compose_overlay((1600, 900), hud, _RECS,
                               {"tab": False, "shop": False}) is None


def test_compose_gold_only_when_tab_held():
    img = igo.compose_overlay((1600, 900), _HUD, None, {"tab": True, "shop": False})
    assert img is not None and img.size == (1600, 900) and img.mode == "RGBA"
    # Something was painted near the top-centre (the gold card), nothing at far left.
    assert img.getpixel((800, 70))[3] > 0 or _any_opaque(img, 700, 60, 900, 130)


def test_compose_gold_hidden_without_tab():
    # Skill point is spent and shop closed, so with Tab up there's nothing.
    hud = dict(_HUD, me={"level": 5},
               skill={"next": "W", "ranks": {"Q": 3, "W": 1, "E": 1, "R": 0}})
    assert igo.compose_overlay((1600, 900), hud, None,
                               {"tab": False, "shop": False}) is None


def test_compose_skill_shows_when_point_available():
    hud = {"me": {"level": 6},
           "skill": {"next": "Q", "ranks": {"Q": 2, "W": 1, "E": 1, "R": 1}}}
    img = igo.compose_overlay((1600, 900), hud, None, {"tab": False, "shop": False})
    assert img is not None
    # The skill card sits bottom-centre.
    assert _any_opaque(img, 720, int(0.77 * 900), 880, int(0.83 * 900))


def test_compose_items_only_when_shop_open():
    closed = igo.compose_overlay((1600, 900),
                                 {"me": {"level": 5},
                                  "skill": {"next": "W",
                                            "ranks": {"Q": 3, "W": 1, "E": 1, "R": 0}}},
                                 _RECS, {"tab": False, "shop": False})
    assert closed is None
    open_img = igo.compose_overlay((1600, 900),
                                   {"me": {"level": 5},
                                    "skill": {"next": "W",
                                              "ranks": {"Q": 3, "W": 1, "E": 1, "R": 0}}},
                                   _RECS, {"tab": False, "shop": True})
    assert open_img is not None
    # Item card anchors just right of the left-docked shop (~0.47 W).
    assert _any_opaque(open_img, int(0.47 * 1600), int(0.15 * 900),
                       int(0.62 * 1600), int(0.30 * 900))


def test_compose_all_three_together():
    img = igo.compose_overlay((1920, 1080), _HUD, _RECS, {"tab": True, "shop": True},
                              "classic", "green")
    assert img is not None and img.size == (1920, 1080)


def _any_opaque(img, x0, y0, x1, y1) -> bool:
    crop = img.crop((x0, y0, x1, y1))
    return crop.getextrema()[3][1] > 0   # max alpha in region > 0


# ── shop detector ────────────────────────────────────────────────────────────
class _Grab:
    """Injectable framebuffer: returns whatever image is currently set."""
    def __init__(self, img):
        self.img = img

    def __call__(self, bbox):
        return self.img


_RECT = (0, 0, 1600, 900)


def test_shop_detector_disabled_returns_false():
    det = igo.ShopDetector(config={"enabled": False}, grab_fn=_Grab(Image.new("RGB", (50, 50), (10, 10, 12))))
    assert det.is_open(_RECT) is False


def test_shop_detector_calibrate_then_match():
    shop_img = Image.new("RGB", (120, 90), (14, 12, 20))
    grab = _Grab(shop_img)
    det = igo.ShopDetector(grab_fn=grab)
    cfg = det.calibrate(_RECT)
    assert cfg and cfg["fingerprint"] and det.calibrated
    # Same frame -> shop detected open.
    assert det.is_open(_RECT) is True
    # A bright, unrelated frame (game world) -> not open.
    grab.img = Image.new("RGB", (120, 90), (180, 200, 120))
    assert det.is_open(_RECT) is False


def test_shop_detector_heuristic_without_fingerprint():
    # Uncalibrated: a dark flat panel reads as "shop open"...
    det = igo.ShopDetector(grab_fn=_Grab(Image.new("RGB", (80, 60), (18, 16, 22))))
    assert det.is_open(_RECT) is True
    # ...a bright, busy frame does not.
    det2 = igo.ShopDetector(grab_fn=_Grab(Image.new("RGB", (80, 60), (150, 170, 120))))
    assert det2.is_open(_RECT) is False


def test_shop_detector_region_bbox_scales_with_window():
    det = igo.ShopDetector()
    x0, y0, x1, y1 = det.region_bbox((100, 200, 1700, 1100))   # 1600x900 at (100,200)
    assert x0 >= 100 and y0 >= 200 and x1 > x0 and y1 > y0
    assert x1 <= 1700 and y1 <= 1100


# ── controller signature (dedupe) ────────────────────────────────────────────
def test_controller_signature_changes_with_gates():
    sig_a = igo.InGameOverlayController._signature(1600, 900, _HUD, _RECS,
                                                   {"tab": True, "shop": False})
    sig_b = igo.InGameOverlayController._signature(1600, 900, _HUD, _RECS,
                                                   {"tab": False, "shop": False})
    assert sig_a != sig_b
    sig_c = igo.InGameOverlayController._signature(1600, 900, _HUD, _RECS,
                                                   {"tab": True, "shop": False})
    assert sig_a == sig_c


# ── bridge wiring ────────────────────────────────────────────────────────────
def _api():
    from unittest.mock import MagicMock
    api = bridge.Api.__new__(bridge.Api)
    api.pusher = bridge.Pusher()
    api.snap = bridge.Api._idle_snapshot()
    api.overrides = MagicMock()
    api.overrides.settings = {"interface_style": "standard", "phosphor": "amber"}
    api.ingame_overlay_active = False
    api.shop_detector = None
    return api


def test_get_ingame_overlay_state_shape():
    api = _api()
    api.snap["hud"] = {"me": {"level": 4}}
    api.snap["itemRecs"] = {"suggestions": []}
    st = api.get_ingame_overlay_state()
    assert st["hud"] == {"me": {"level": 4}}
    assert st["item_recs"] == {"suggestions": []}
    assert st["interface_style"] == "standard" and st["phosphor"] == "amber"


def test_on_ingame_overlay_visibility_pushes():
    api = _api()
    api._on_ingame_overlay_visibility(True)
    assert api.ingame_overlay_active is True
    assert api.snap["ingame_overlay_active"] is True
    evt = api.pusher.drain()[0]
    assert evt["event"] == "ingame_overlay_active" and evt["payload"] == {"active": True}


def test_calibrate_shop_detection_without_detector_errors():
    api = _api()
    r = api.calibrate_shop_detection()
    assert r["ok"] is False and "not initialised" in r["error"]
