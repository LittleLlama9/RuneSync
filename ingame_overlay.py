"""ingame_overlay.py — a true see-through overlay over the League *game* window.

Where overlay.py anchors to the RCLIENT client (champ select), this draws over
the live in-match window (class "RiotWindowClass", title "League of Legends (TM)
Client"). It paints up to three small, League-HUD-conscious indicators onto one
full-window-size, click-through, per-pixel-alpha layered image:

  * GOLD  — team gold lead/deficit, top-centre near the scoreboard, shown ONLY
            while the scoreboard key (Tab) is physically held.
  * SKILL — which ability to level next, bottom-centre above the ability bar,
            shown ONLY when a skill point is unspent (level > points spent).
  * ITEMS — defensive item recs, beside the shop panel, shown ONLY while the
            shop is open (detected by sampling a calibrated screen region — the
            Live Client Data API exposes no shop-open signal).

Policy mirrors overlay.py: read-only (it only surfaces data the monitor already
produced from the sanctioned :2999 API), click-through, always-on-top, and
opt-in (shown only while a game window is on screen). Requires the game in
Borderless/Windowed — a layered window cannot draw over exclusive fullscreen.

The pure helpers (`skill_point_available`, `fmt_gold`, `compose_overlay`,
`ShopDetector`) are unit-tested with injected inputs; the `InGameOverlayController`
anchor loop and live key/screen sampling are exercised at runtime.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from overlay import (
    _HAVE_PIL, _HAVE_WIN32, _font, _palette, _vgrad,
    LayeredOverlay, find_game_window,
)

try:  # pragma: no cover - import guarded exactly like overlay.py
    from PIL import Image, ImageDraw, ImageFilter  # type: ignore
except Exception:  # pragma: no cover
    Image = ImageDraw = ImageFilter = None  # type: ignore

try:  # pragma: no cover - win32 present only on the target OS
    import win32gui  # type: ignore
except Exception:  # pragma: no cover
    win32gui = None  # type: ignore

_POLL_INTERVAL = 0.25          # Tab responsiveness vs. idle cost
_VK_TAB = 0x09

# Indicator anchors as (fx, fy, ax, ay): the card's own anchor point (ax, ay) in
# [0,1] is placed at (fx*W, fy*H) of the game window. Center-top = (0.5, 0.0).
# Tuned to the default League HUD; overridable per-indicator via settings later.
_ANCHORS = {
    "gold":  (0.500, 0.070, 0.5, 0.0),   # top-centre, over the scoreboard header
    "skill": (0.500, 0.775, 0.5, 0.0),   # bottom-centre, above the Q/W/E/R bar
    "items": (0.468, 0.150, 0.0, 0.0),   # just right of the (left-docked) shop
}

# Default shop-sample region as fractions of the game window: the left panel the
# shop occupies. Sampled for a calibrated fingerprint (see ShopDetector).
_DEFAULT_SHOP_REGION = (0.010, 0.120, 0.440, 0.760)   # (rx, ry, rw, rh)
_SHOP_GRID = (7, 5)            # sample columns x rows across the region
_SHOP_TOL = 46                 # per-point RGB Euclidean match tolerance
_SHOP_MATCH = 0.62             # fraction of points that must match to be "open"


# ── pure state helpers ───────────────────────────────────────────────────────
def skill_point_available(hud: Optional[dict]) -> bool:
    """True when the local player has an unspent skill point (just levelled and
    hasn't picked an ability yet). Derived from level minus Q/W/E/R ranks."""
    if not hud:
        return False
    sk = hud.get("skill")
    if not sk or not sk.get("next"):
        return False
    ranks = sk.get("ranks") or {}
    try:
        spent = sum(int(ranks.get(k, 0) or 0) for k in ("Q", "W", "E", "R"))
        level = int((hud.get("me") or {}).get("level") or 0)
    except (TypeError, ValueError):
        return False
    return (level - spent) > 0


def fmt_gold(n: int) -> str:
    """Compact signed gold: +1.2k / -850 / +0."""
    try:
        n = int(round(n))
    except (TypeError, ValueError):
        n = 0
    sign = "+" if n >= 0 else "-"
    a = abs(n)
    body = f"{a/1000:.1f}k" if a >= 1000 else str(a)
    return f"{sign}{body}"


def read_tab_held(get_key=None) -> bool:
    """True while the scoreboard key (Tab) is physically down. Passive read via
    GetAsyncKeyState (no hook, no injection); `get_key` injectable for tests."""
    if get_key is not None:
        try:
            return bool(get_key(_VK_TAB) & 0x8000)
        except Exception:
            return False
    if not _HAVE_WIN32:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(_VK_TAB) & 0x8000)
    except Exception:
        return False


# ── card rendering ───────────────────────────────────────────────────────────
_PAD = 8   # shadow-bleed margin baked into every card image


def _new_card(w: int, h: int, pal: dict):
    """A rounded, translucent gradient card (matching the app skin) with a soft
    drop shadow. Returns (image, draw, inset) where inset is the shadow margin."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [_PAD - 1, _PAD + 2, w - _PAD + 1, h - _PAD + 3], radius=10,
        fill=(0, 0, 0, 150))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(5)))
    bw, bh = w - _PAD * 2, h - _PAD * 2
    grad = _vgrad(bw, bh, pal["bg_top"], pal["bg_bot"], pal["alpha"])
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=9, fill=255)
    img.paste(grad, (_PAD, _PAD), mask)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([_PAD, _PAD, w - _PAD - 1, h - _PAD - 1], radius=9,
                        outline=pal["border"], width=1)
    return img, d, _PAD


def _gold_card(pal: dict, team_diff: int) -> "Image.Image":
    f_lab = _font("semi", 10)
    f_val = _font("title", 19)
    val = fmt_gold(team_diff)
    color = pal["good"] if team_diff >= 0 else pal["bad"]
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lab_w = dummy.textlength("TEAM GOLD", font=f_lab)
    val_w = dummy.textlength(val, font=f_val)
    inner = int(max(lab_w, val_w)) + 28
    w = inner + _PAD * 2
    h = 52 + _PAD * 2
    img, d, p = _new_card(w, h, pal)
    cx = w / 2
    d.text((cx, p + 12), "TEAM GOLD", font=f_lab, fill=pal["muted"], anchor="mm")
    d.text((cx, p + 33), val, font=f_val, fill=color, anchor="mm")
    return img


def _skill_card(pal: dict, skill: dict) -> "Image.Image":
    nxt = str(skill.get("next") or "").upper()
    ranks = skill.get("ranks") or {}
    f_lab = _font("semi", 10)
    f_key = _font("title", 22)
    f_rank = _font("regular", 11)
    ranks_txt = " ".join(f"{k}{int(ranks.get(k, 0) or 0)}" for k in ("Q", "W", "E", "R"))
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    rank_w = dummy.textlength(ranks_txt, font=f_rank)
    badge = 30
    inner = badge + 12 + int(max(rank_w, dummy.textlength("LEVEL UP", font=f_lab))) + 16
    w = inner + _PAD * 2
    h = 48 + _PAD * 2
    img, d, p = _new_card(w, h, pal)
    # Ability-key badge (the key to press), then the label + current ranks.
    bx = p + 12
    by = h / 2
    d.rounded_rectangle([bx, by - badge / 2, bx + badge, by + badge / 2],
                        radius=7, fill=pal["accent"])
    d.text((bx + badge / 2, by), nxt, font=f_key, fill=pal["bg_bot"], anchor="mm")
    tx = bx + badge + 12
    d.text((tx, p + 12), "LEVEL UP", font=f_lab, fill=pal["muted"], anchor="lm")
    d.text((tx, p + 30), ranks_txt, font=f_rank, fill=pal["text"], anchor="lm")
    return img


def _items_card(pal: dict, recs: dict) -> Optional["Image.Image"]:
    suggestions = [s for s in (recs.get("suggestions") or [])
                   if isinstance(s, dict)][:3]
    notes = [n for n in (recs.get("notes") or []) if n][:1]
    if not suggestions and not notes:
        return None
    f_head = _font("semi", 10)
    f_reason = _font("semi", 11)
    f_item = _font("regular", 11)
    f_note = _font("regular", 11)
    lines: list[tuple] = []   # (kind, text)
    for s in suggestions:
        reason = str(s.get("reason") or "").strip()
        items = " · ".join(str(it.get("name") or "") for it in (s.get("items") or [])
                           if isinstance(it, dict) and it.get("name"))
        if reason:
            lines.append(("reason", reason))
        if items:
            lines.append(("item", items))
    for n in notes:
        lines.append(("note", str(n)))
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    fonts = {"reason": f_reason, "item": f_item, "note": f_note}
    text_w = max((dummy.textlength(t, font=fonts[k]) for k, t in lines), default=80)
    inner = int(max(text_w, dummy.textlength("DEFENSIVE BUYS", font=f_head))) + 26
    w = min(inner, 260) + _PAD * 2
    header_h = 22
    row_h = 17
    h = header_h + row_h * len(lines) + 14 + _PAD * 2
    img, d, p = _new_card(w, h, pal)
    tx = p + 13
    d.text((tx, p + 12), "DEFENSIVE BUYS", font=f_head, fill=pal["accent"], anchor="lm")
    y = p + header_h + 8
    maxw = w - _PAD * 2 - 26
    for k, t in lines:
        if k == "reason":
            d.ellipse([tx, y + 5, tx + 4, y + 9], fill=pal["bullet"])
            d.text((tx + 12, y), _clip(d, t, fonts[k], maxw - 12),
                   font=fonts[k], fill=pal["text"], anchor="lm")
        elif k == "item":
            d.text((tx + 12, y), _clip(d, t, fonts[k], maxw - 12),
                   font=fonts[k], fill=pal["accent_br"], anchor="lm")
        else:
            d.text((tx, y), _clip(d, t, fonts[k], maxw), font=fonts[k],
                   fill=pal["muted"], anchor="lm")
        y += row_h
    return img


def _clip(draw, text: str, font, max_w: int) -> str:
    if not text or draw.textlength(text, font=font) <= max_w:
        return text or ""
    ell = "…"
    while text and draw.textlength(text + ell, font=font) > max_w:
        text = text[:-1]
    return (text + ell) if text else ell


def _place(card: "Image.Image", spec, W: int, H: int) -> tuple[int, int]:
    fx, fy, ax, ay = spec
    cw, ch = card.size
    x = int(fx * W - ax * cw)
    y = int(fy * H - ay * ch)
    x = max(0, min(x, W - cw))
    y = max(0, min(y, H - ch))
    return x, y


def compose_overlay(game_size, hud: Optional[dict], item_recs: Optional[dict],
                    flags: dict, interface: str = "standard",
                    phosphor: str = "amber") -> Optional["Image.Image"]:
    """Compose the active indicators onto a transparent, game-sized RGBA image,
    or None when nothing should show (so the controller can cheaply hide).

    `flags` carries the runtime gates the controller resolved: {"tab": bool,
    "shop": bool}. Pure/deterministic given its inputs — no key or screen access
    here — so it is unit-tested directly.
    """
    if not _HAVE_PIL:
        return None
    W, H = int(game_size[0]), int(game_size[1])
    if W <= 0 or H <= 0:
        return None
    pal = _palette(interface, phosphor)

    cards = []   # (name, image)
    # GOLD — only while the scoreboard is up, and only if we have a team total.
    if flags.get("tab") and hud:
        tg = hud.get("team_gold")
        if isinstance(tg, dict) and tg.get("diff") is not None:
            cards.append(("gold", _gold_card(pal, int(tg.get("diff") or 0))))
    # SKILL — only when a skill point is waiting to be spent.
    if skill_point_available(hud):
        cards.append(("skill", _skill_card(pal, hud["skill"])))
    # ITEMS — only while the shop is open.
    if flags.get("shop") and isinstance(item_recs, dict):
        card = _items_card(pal, item_recs)
        if card is not None:
            cards.append(("items", card))

    if not cards:
        return None

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for name, card in cards:
        canvas.alpha_composite(card, _place(card, _ANCHORS[name], W, H))
    return canvas


# ── shop detection (screen-region pixel sampling) ────────────────────────────
class ShopDetector:
    """Infers whether the in-game shop is open by sampling a region of the
    *desktop* framebuffer (never game memory) and matching it against a
    calibrated fingerprint of the shop's chrome.

    Config (persisted in settings under "shop_detect"):
      enabled     : bool
      region      : (rx, ry, rw, rh) fractions of the game window to sample
      fingerprint : [[fx, fy, r, g, b], ...] reference colours captured with the
                    shop open (points are fractions *within* the region)
      tol         : per-point RGB Euclidean match tolerance
      match       : fraction of points that must match to call the shop "open"

    With no fingerprint it falls back to a conservative "dark, flat panel"
    heuristic and still works, but calibration (one capture with the shop open)
    makes it reliable. `grab_fn(bbox)->RGB image` is injectable for tests.
    """

    def __init__(self, config: Optional[dict] = None, grab_fn=None):
        self.config = dict(config or {})
        self._grab = grab_fn

    def _grab_region(self, bbox):
        if self._grab is not None:
            return self._grab(bbox)
        from PIL import ImageGrab  # lazy: only needed live
        return ImageGrab.grab(bbox=bbox)

    def region_bbox(self, game_rect) -> tuple[int, int, int, int]:
        left, top, right, bottom = game_rect
        W, H = right - left, bottom - top
        rx, ry, rw, rh = self.config.get("region") or _DEFAULT_SHOP_REGION
        x0 = left + int(rx * W)
        y0 = top + int(ry * H)
        x1 = x0 + max(1, int(rw * W))
        y1 = y0 + max(1, int(rh * H))
        return (x0, y0, x1, y1)

    def is_open(self, game_rect) -> bool:
        if not self.config.get("enabled", True):
            return False
        try:
            img = self._grab_region(self.region_bbox(game_rect)).convert("RGB")
        except Exception:
            return False
        fp = self.config.get("fingerprint")
        if fp:
            return self._match(img, fp)
        return self._heuristic(img)

    def _match(self, img, fingerprint) -> bool:
        w, h = img.size
        if w <= 0 or h <= 0:
            return False
        px = img.load()
        tol = float(self.config.get("tol", _SHOP_TOL))
        need = float(self.config.get("match", _SHOP_MATCH))
        hits = 0
        total = 0
        for entry in fingerprint:
            try:
                fx, fy, r, g, b = entry
            except (TypeError, ValueError):
                continue
            total += 1
            x = min(w - 1, max(0, int(fx * w)))
            y = min(h - 1, max(0, int(fy * h)))
            cr, cg, cb = px[x, y][:3]
            if ((cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2) ** 0.5 <= tol:
                hits += 1
        return total > 0 and (hits / total) >= need

    def _heuristic(self, img) -> bool:
        """Uncalibrated fallback: the open shop paints a large, dark, flat panel
        over the (brighter, busier) game world in this region. Conservative on
        purpose — false positives are worse than a missed frame."""
        small = img.resize((24, 16))
        data = small.tobytes()   # RGB triples, avoids deprecated getdata()
        n = len(data) // 3
        if n <= 0:
            return False
        bright = [(data[i] + data[i + 1] + data[i + 2]) / 3.0
                  for i in range(0, n * 3, 3)]
        mean = sum(bright) / len(bright)
        dark_frac = sum(1 for v in bright if v < 60) / len(bright)
        return mean < 85 and dark_frac >= 0.55

    def calibrate(self, game_rect) -> Optional[dict]:
        """Capture the current shop region as the reference fingerprint. Call
        this with the shop OPEN in a live game; returns the config to persist (or
        None if the grab failed)."""
        try:
            img = self._grab_region(self.region_bbox(game_rect)).convert("RGB")
        except Exception:
            return None
        w, h = img.size
        if w <= 0 or h <= 0:
            return None
        px = img.load()
        cols, rows = _SHOP_GRID
        fingerprint = []
        for j in range(rows):
            for i in range(cols):
                fx = (i + 0.5) / cols
                fy = (j + 0.5) / rows
                x = min(w - 1, int(fx * w))
                y = min(h - 1, int(fy * h))
                r, g, b = px[x, y][:3]
                fingerprint.append([round(fx, 4), round(fy, 4), r, g, b])
        self.config = {
            **self.config,
            "enabled": True,
            "region": list(self.config.get("region") or _DEFAULT_SHOP_REGION),
            "fingerprint": fingerprint,
            "tol": self.config.get("tol", _SHOP_TOL),
            "match": self.config.get("match", _SHOP_MATCH),
        }
        return self.config

    @property
    def calibrated(self) -> bool:
        return bool(self.config.get("fingerprint"))


# ── controller ───────────────────────────────────────────────────────────────
class InGameOverlayController:
    """Owns the in-game anchor loop: track the game window, resolve the runtime
    gates (Tab held, shop open, skill point waiting), compose the active
    indicators and blit them over the game.

    `state_provider()` returns {"hud", "item_recs", "interface_style",
    "phosphor"} (see bridge.get_ingame_overlay_state). `should_show()` gates the
    whole overlay (wired to running & in-game). All work is best-effort; a hiccup
    never disturbs monitoring. `on_visibility(bool)` fires once per change so the
    app window can collapse its now-redundant in-game panels.
    """

    def __init__(self, state_provider: Callable[[], dict],
                 should_show: Callable[[], bool],
                 shop_detector: Optional[ShopDetector] = None,
                 on_visibility: Optional[Callable[[bool], None]] = None,
                 key_fn=None):
        self._state_provider = state_provider
        self._should_show = should_show
        self._shop = shop_detector or ShopDetector()
        self._on_visibility = on_visibility
        self._key_fn = key_fn
        self._overlay = LayeredOverlay()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_visible: Optional[bool] = None
        self._last_sig = None

    def _set_visible(self, visible: bool):
        if visible == self._last_visible:
            return
        self._last_visible = visible
        if self._on_visibility is not None:
            try:
                self._on_visibility(visible)
            except Exception:
                pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ingame-overlay")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(_POLL_INTERVAL):
            try:
                self._tick()
            except Exception:
                pass
        try:
            self._overlay.destroy()
        except Exception:
            pass
        self._set_visible(False)

    def _tick(self):
        if _HAVE_WIN32 and win32gui is not None:
            try:
                win32gui.PumpWaitingMessages()
            except Exception:
                pass

        want = False
        try:
            want = bool(self._should_show())
        except Exception:
            want = False

        hwnd = find_game_window() if want else None
        if want and hwnd is None:
            want = False

        if not want:
            self._overlay.hide()
            self._set_visible(False)
            self._last_sig = None
            return

        try:
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            self._overlay.hide()
            self._set_visible(False)
            return
        gw, gh = rect[2] - rect[0], rect[3] - rect[1]
        if gw <= 0 or gh <= 0:
            self._overlay.hide()
            self._set_visible(False)
            return

        try:
            st = self._state_provider() or {}
        except Exception:
            st = {}
        hud = st.get("hud")
        item_recs = st.get("item_recs")
        interface = st.get("interface_style") or "standard"
        phosphor = st.get("phosphor") or "amber"

        tab = read_tab_held(self._key_fn)
        shop = False
        if isinstance(item_recs, dict) and item_recs.get("suggestions"):
            try:
                shop = self._shop.is_open(rect)
            except Exception:
                shop = False
        flags = {"tab": tab, "shop": shop}

        # Cheap change signature: skip the full-size compose+blit when nothing
        # that affects the picture changed and we're already shown.
        sig = self._signature(gw, gh, hud, item_recs, flags)
        if sig == self._last_sig and self._last_visible:
            return

        img = compose_overlay((gw, gh), hud, item_recs, flags, interface, phosphor)
        if img is None:
            self._overlay.hide()
            self._set_visible(False)
            self._last_sig = sig
            return

        if not self._overlay.ensure():
            self._set_visible(False)
            return
        if self._overlay.blit(img, rect[0], rect[1]):
            self._overlay.show()
            self._set_visible(True)
            self._last_sig = sig

    @staticmethod
    def _signature(gw, gh, hud, item_recs, flags):
        sk = (hud or {}).get("skill") or {}
        tg = (hud or {}).get("team_gold") or {}
        avail = skill_point_available(hud)
        n_sug = len(((item_recs or {}).get("suggestions")) or []) if flags.get("shop") else 0
        return (
            gw, gh, bool(flags.get("tab")), bool(flags.get("shop")),
            int(tg.get("diff") or 0) if flags.get("tab") else None,
            sk.get("next") if avail else None,
            tuple(sorted((sk.get("ranks") or {}).items())) if avail else None,
            n_sug,
        )
