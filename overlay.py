"""overlay.py — a true see-through champ-select overlay over the League client.

Unlike the main app (a WebView2 window), this is a native Win32 *layered*
window with per-pixel alpha (UpdateLayeredWindow). That buys us what WebView2
can't do: a semi-transparent panel with crisp text painted directly over the
champ-select art, click-through (mouse events pass to the client), and
always-on-top — the Blitz/Mobalytics style overlay.

Design:
  * read-only — it renders champ-select data the monitor already produces
    (matchup win rate, counter picks, draft read); it never drives picks/bans,
    so it stays within Riot's policy.
  * click-through — WS_EX_TRANSPARENT so it never intercepts clicks meant for
    the client. There's no drag handle by design (that would need input).
  * opt-in visibility — shown only while the user is in champ select AND the
    League client window is on screen; hidden otherwise.
  * defensive — every Windows call is guarded; if win32/GDI/Pillow are missing
    the overlay simply stays hidden and the rest of the app is unaffected.

The pure helpers (`overlay_anchor`, `find_client_window`, `render_panel`) are
unit-tested; the `LayeredOverlay` GDI plumbing and `OverlayController` anchor
loop are exercised at runtime.
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Callable, Optional

try:
    import win32gui  # type: ignore
    import win32con  # type: ignore
    _HAVE_WIN32 = True
except Exception:  # pragma: no cover - non-Windows / missing pywin32
    _HAVE_WIN32 = False

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter  # type: ignore
    _HAVE_PIL = True
except Exception:  # pragma: no cover
    _HAVE_PIL = False

# The League client (lobby / champ select / post-game) renders in a window whose
# class is "RCLIENT". The in-game process uses a different class, so matching on
# RCLIENT keeps the overlay tied to the client where champ select actually lives.
_CLIENT_WINDOW_CLASS = "RCLIENT"
_CLIENT_WINDOW_TITLE = "League of Legends"

# PANEL_WIDTH is the full rendered image width (the visible panel is inset by
# _POUT on every side to leave room for the drop shadow).
PANEL_WIDTH = 320
_POUT = 16                 # shadow / bleed margin around the panel
_MARGIN = 8
_TOP_OFFSET = 64           # clear the client's top bar
_POLL_INTERVAL = 0.5

# ── palette (League "hextech": gold trim, hextech teal, deep navy) ────────────
# Sourced from the client's own Riot palette so the overlay reads as native.
_GOLD    = (200, 170, 110)   # C8AA6E  primary gold trim / labels
_GOLD_BR = (240, 230, 210)   # F0E6D2  bright gold, headline text
_GOLD_DK = (120, 92, 46)     # 785C2E  dark gold hairline / dividers
_TEAL    = (10, 200, 185)    # 0AC8B9  hextech accent (bullets, bars)
_TEXT    = (224, 220, 205)   # warm off-white body text
_MUTED   = (155, 160, 150)   # A0A096  secondary / captions
_GOOD    = (72, 200, 140)    # favourable win rate
_BAD     = (214, 92, 108)    # unfavourable win rate
_BG_TOP  = (20, 33, 54)      # panel gradient top (navy)
_BG_BOT  = (8, 16, 30)       # panel gradient bottom (near-black navy)
_PANEL_A = 235               # panel opacity (0-255)
_TRACK   = (255, 255, 255, 30)   # progress-bar track


def _asset(*parts) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


_FONTS: dict = {}
_WIN_FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
# Segoe UI is present on every Win10/11 machine and reads as "client-native".
# Fall back to the bundled SpaceMono so a stripped environment still renders.
_FONT_FILES = {
    "title":   ["seguisb.ttf", "segoeui.ttf"],
    "semi":    ["seguisb.ttf", "segoeui.ttf"],
    "regular": ["segoeui.ttf"],
}


def _font(kind: str, size: int):
    """Load a UI font by role ('title'|'semi'|'regular'). Cached per (kind,size)."""
    key = (kind, size)
    if key in _FONTS:
        return _FONTS[key]
    f = None
    for name in _FONT_FILES.get(kind, ["segoeui.ttf"]):
        p = os.path.join(_WIN_FONTS, name)
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                break
            except Exception:
                f = None
    if f is None:
        bundled = "SpaceMono-Regular.ttf" if kind == "regular" else "SpaceMono-Bold.ttf"
        try:
            f = ImageFont.truetype(_asset("webui", "fonts", bundled), size)
        except Exception:
            try:
                f = ImageFont.load_default()
            except Exception:
                f = None
    _FONTS[key] = f
    return f


def _vgrad(w: int, h: int, top, bot, alpha: int):
    """A vertical top→bot gradient RGBA image of size (w, h) at constant alpha."""
    h = max(1, h)
    strip = Image.new("RGBA", (1, h))
    px = strip.load()
    denom = max(1, h - 1)
    for yy in range(h):
        t = yy / denom
        px[0, yy] = (
            int(top[0] + (bot[0] - top[0]) * t),
            int(top[1] + (bot[1] - top[1]) * t),
            int(top[2] + (bot[2] - top[2]) * t),
            alpha,
        )
    return strip.resize((max(1, w), h))


def _diamond(d, cx, cy, r, fill):
    d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=fill)


def _corner(d, x, y, dx, dy, ln, col, w=2):
    """An L-shaped corner bracket at (x, y) opening toward (dx, dy) ∈ {-1,1}."""
    d.line([(x, y), (x + dx * ln, y)], fill=col, width=w)
    d.line([(x, y), (x, y + dy * ln)], fill=col, width=w)


# ── geometry ─────────────────────────────────────────────────────────────────
def overlay_anchor(client_rect, panel_w: int, panel_h: int,
                   screen_x: int = 0, screen_y: int = 0,
                   screen_w: Optional[int] = None, screen_h: Optional[int] = None,
                   margin: int = _MARGIN, top_offset: int = _TOP_OFFSET):
    """Top-right anchor (x, y) for the overlay, docked inside the client's right
    edge just below its top bar — where a companion overlay lives.

    `client_rect` is (left, top, right, bottom) in screen pixels. When screen
    bounds are given the result is clamped to the (virtual) desktop, whose
    origin can be negative on multi-monitor layouts (monitors left of / above
    the primary), so clamping is done within
    [screen_x, screen_x+screen_w] × [screen_y, screen_y+screen_h].
    """
    left, top, right, bottom = client_rect
    x = right - panel_w - margin
    y = top + top_offset
    if screen_w is not None and screen_h is not None:
        sx1, sy1 = screen_x + screen_w, screen_y + screen_h
        x = max(screen_x, min(x, sx1 - panel_w))
        y = max(screen_y, min(y, sy1 - panel_h))
    return x, y


def find_client_window():
    """HWND of the visible League client window, or None.

    Matches on window class first (robust to title localisation) and falls back
    to the English title. Returns only a visible, non-minimised window.
    """
    if not _HAVE_WIN32:
        return None
    found = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if cls == _CLIENT_WINDOW_CLASS or title == _CLIENT_WINDOW_TITLE:
                rect = win32gui.GetWindowRect(hwnd)
                # Skip zero-size / minimised windows (rect goes far negative).
                if rect[2] - rect[0] > 100 and rect[3] - rect[1] > 100:
                    found.append(hwnd)
        except Exception:
            pass

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return None
    return found[0] if found else None


def _screen_bounds():
    """Virtual-desktop bounds as (x, y, w, h). The origin can be negative when a
    monitor sits left of / above the primary display."""
    if not _HAVE_WIN32:
        return 0, 0, 1920, 1080
    try:
        return (win32gui.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN) or 0,
                win32gui.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN) or 0,
                win32gui.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN) or 1920,
                win32gui.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN) or 1080)
    except Exception:
        return 0, 0, 1920, 1080


# ── rendering ────────────────────────────────────────────────────────────────
def _fit(draw, text: str, font, max_w: int) -> str:
    """Truncate `text` with an ellipsis so it fits within max_w pixels."""
    if font is None or not text:
        return text or ""
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    while text and draw.textlength(text + ell, font=font) > max_w:
        text = text[:-1]
    return (text + ell) if text else ell


def _has_matchup(st: dict) -> bool:
    return bool(st.get("enemy")) and (st.get("wr") is not None or st.get("wrLabel"))


def _counters_of(st: dict):
    c = st.get("counters")
    if isinstance(c, dict) and c.get("active") and c.get("counters"):
        return c
    return None


def _draft_of(st: dict):
    d = st.get("draft")
    if isinstance(d, dict) and d.get("observations"):
        return d
    return None


def render_panel(state, theme: str = "amber", width: int = PANEL_WIDTH):
    """Render the overlay panel to an RGBA image, or None if there's nothing to
    show yet (state is falsy). Always renders at least a header while in champ
    select so the user can see the overlay is live.

    The look is modelled on the League client itself: a deep-navy hextech panel
    with a gold border, gold corner brackets, a filigree divider under the
    wordmark, teal section bullets, and win-rate bars. `width` is the full image
    width; the visible panel is inset by `_POUT` on every side for the shadow.
    """
    if not state or not _HAVE_PIL:
        return None

    # ── fonts ──
    f_title = _font("title", 17)
    f_tag   = _font("semi", 9)
    f_label = _font("semi", 10)
    f_champ = _font("semi", 15)
    f_wr    = _font("title", 22)
    f_body  = _font("regular", 12)
    f_small = _font("regular", 12)

    pout = _POUT
    pw = width - pout * 2               # visible panel width
    padx = 16
    tx = pout + padx                    # left text edge
    rx = pout + pw - padx               # right text edge
    inner_w = pw - padx * 2

    HEAD_H = 52                         # wordmark + filigree divider

    # ── measure pass: flat item list, each carrying its own height ──
    items = []
    ih = HEAD_H

    def add(kind, payload, h):
        nonlocal ih
        items.append((kind, payload, h))
        ih += h

    mu = _has_matchup(state)
    co = _counters_of(state)
    dr = _draft_of(state)

    if mu:
        add("section", "MATCHUP", 22)
        add("champvs", (state.get("champ") or "You", state.get("enemy") or "?"), 24)
        add("wr", state, 44)

    if co:
        enemy = (co.get("enemy") or "").strip()
        add("section", f"COUNTERS · {enemy}" if enemy else "COUNTERS", 22)
        for row in (co.get("counters") or [])[:4]:
            add("counter", row, 27)

    if dr:
        add("section", "TEAM DRAFT", 22)
        for ob in (dr.get("observations") or [])[:3]:
            # Prefer the compact overlay phrasing; fall back to the full text
            # (which the main window shows in full).
            if isinstance(ob, dict):
                txt = ob.get("short") or ob.get("text")
            else:
                txt = str(ob)
            add("obs", txt, 21)

    if not (mu or co or dr):
        add("hint", "Analyzing draft…", 22)

    panel_h = ih + 16                   # bottom padding
    img_w = width
    img_h = panel_h + pout * 2

    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))

    # ── drop shadow ──
    shadow = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle([pout - 2, pout + 3, pout + pw + 2, pout + panel_h + 6],
                         radius=16, fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(7))
    img.alpha_composite(shadow)

    # ── panel body: navy gradient clipped to a rounded rect ──
    px0, py0 = pout, pout
    px1, py1 = pout + pw, pout + panel_h
    grad = _vgrad(pw, panel_h, _BG_TOP, _BG_BOT, _PANEL_A)
    mask = Image.new("L", (pw, panel_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, pw - 1, panel_h - 1],
                                           radius=14, fill=255)
    img.paste(grad, (px0, py0), mask)

    d = ImageDraw.Draw(img)

    # subtle top sheen so the panel isn't flat
    d.rounded_rectangle([px0 + 1, py0 + 1, px1 - 1, py0 + 22],
                        radius=13, fill=(255, 255, 255, 12))
    d.rectangle([px0 + 1, py0 + 12, px1 - 1, py0 + 22], fill=(0, 0, 0, 0))

    # ── border: dark gold frame + bright inner hairline ──
    d.rounded_rectangle([px0, py0, px1 - 1, py1 - 1], radius=14,
                        outline=_GOLD_DK + (255,), width=1)
    d.rounded_rectangle([px0 + 1, py0 + 1, px1 - 2, py1 - 2], radius=13,
                        outline=_GOLD + (70,), width=1)

    # ── gold corner brackets ──
    cl = 14
    _corner(d, px0 + 6, py0 + 6,  1,  1, cl, _GOLD_BR + (235,))
    _corner(d, px1 - 6, py0 + 6, -1,  1, cl, _GOLD_BR + (235,))
    _corner(d, px0 + 6, py1 - 6,  1, -1, cl, _GOLD_BR + (235,))
    _corner(d, px1 - 6, py1 - 6, -1, -1, cl, _GOLD_BR + (235,))

    # ── header: wordmark + phase tag + filigree divider ──
    d.text((tx, py0 + 13), "RUNE", font=f_title, fill=_GOLD_BR + (255,))
    rw = d.textlength("RUNE", font=f_title)
    d.text((tx + rw, py0 + 13), "SYNC", font=f_title, fill=_GOLD + (255,))
    tag = "CHAMP SELECT"
    if f_tag is not None:
        tw = d.textlength(tag, font=f_tag)
        d.text((rx - tw, py0 + 18), tag, font=f_tag, fill=_MUTED + (255,))
    dv = py0 + 40
    midx = (tx + rx) // 2
    d.line([(tx, dv), (midx - 6, dv)], fill=_GOLD_DK + (200,), width=1)
    d.line([(midx + 6, dv), (rx, dv)], fill=_GOLD_DK + (200,), width=1)
    _diamond(d, midx, dv, 3, _GOLD + (255,))

    # ── draw pass ──
    cy = py0 + HEAD_H
    for kind, payload, h in items:
        if kind == "section":
            _diamond(d, tx + 3, cy + 8, 3, _TEAL + (255,))
            d.text((tx + 13, cy), _fit(d, payload, f_label, inner_w - 13),
                   font=f_label, fill=_GOLD + (235,))
            lw = d.textlength(payload, font=f_label)
            lxs = tx + 13 + lw + 8
            if lxs < rx:
                d.line([(lxs, cy + 8), (rx, cy + 8)], fill=_GOLD_DK + (110,), width=1)

        elif kind == "champvs":
            champ, enemy = payload
            champ = (champ or "You")
            enemy = (enemy or "?")
            d.text((tx, cy), _fit(d, champ, f_champ, inner_w - 70),
                   font=f_champ, fill=_GOLD_BR + (255,))
            cw = d.textlength(_fit(d, champ, f_champ, inner_w - 70), font=f_champ)
            vx = tx + cw + 8
            d.text((vx, cy + 3), "vs", font=f_small, fill=_MUTED + (255,))
            vw = d.textlength("vs", font=f_small)
            ex = vx + vw + 8
            d.text((ex, cy), _fit(d, enemy, f_champ, rx - ex),
                   font=f_champ, fill=_TEXT + (255,))

        elif kind == "wr":
            st = payload
            wr = st.get("wr")
            lab = (st.get("wrLabel") or "").strip()
            has_wr = isinstance(wr, (int, float))
            col = _GOOD if (has_wr and wr >= 50) else _BAD if has_wr else _MUTED
            wr_txt = f"{wr:.1f}%" if has_wr else "—"
            d.text((tx, cy), wr_txt, font=f_wr, fill=col + (255,))
            numw = d.textlength(wr_txt, font=f_wr)
            bx0 = tx + numw + 14
            bx1 = rx
            by = cy + 13
            if bx1 - bx0 > 20:
                d.rounded_rectangle([bx0, by, bx1, by + 7], radius=3, fill=_TRACK)
                frac = max(0.0, min(1.0, (wr / 100.0) if has_wr else 0.0))
                if frac > 0:
                    fillw = bx0 + int((bx1 - bx0) * frac)
                    d.rounded_rectangle([bx0, by, max(bx0 + 3, fillw), by + 7],
                                        radius=3, fill=col + (240,))
            if lab:
                lw = d.textlength(lab, font=f_small)
                d.text((rx - lw, cy + 24), _fit(d, lab, f_small, inner_w),
                       font=f_small, fill=col + (235,))

        elif kind == "counter":
            row = payload
            name = row.get("champion", "") if isinstance(row, dict) else str(row)
            wr = row.get("win_rate") if isinstance(row, dict) else None
            has_wr = isinstance(wr, (int, float))
            wr_s = f"{wr:.1f}%" if has_wr else ""
            d.text((tx + 4, cy), _fit(d, name, f_body, inner_w - 60),
                   font=f_body, fill=_TEXT + (255,))
            if wr_s:
                tw = d.textlength(wr_s, font=f_body)
                d.text((rx - tw, cy), wr_s, font=f_body, fill=_TEAL + (255,))
            # thin proportional bar (map a plausible 44-60% band to 0-1)
            by = cy + 18
            d.rounded_rectangle([tx + 4, by, rx, by + 4], radius=2, fill=_TRACK)
            if has_wr:
                frac = max(0.05, min(1.0, (wr - 44.0) / 16.0))
                fillw = tx + 4 + int((rx - tx - 4) * frac)
                d.rounded_rectangle([tx + 4, by, max(tx + 7, fillw), by + 4],
                                    radius=2, fill=_TEAL + (220,))

        elif kind == "obs":
            _diamond(d, tx + 4, cy + 7, 2, _TEAL + (235,))
            d.text((tx + 13, cy), _fit(d, payload or "", f_small, inner_w - 13),
                   font=f_small, fill=_TEXT + (235,))

        elif kind == "hint":
            d.text((tx, cy), payload, font=f_small, fill=_MUTED + (255,))

        cy += h

    return img


# ── layered window ───────────────────────────────────────────────────────────
def _to_premultiplied_bgra(img) -> bytes:
    """RGBA Pillow image -> top-down premultiplied BGRA bytes for a 32bpp DIB.

    Pure Pillow (no numpy) so the overlay's only heavy deps stay Pillow +
    pywin32. `ImageChops.multiply` computes channel*alpha/255 per pixel; merging
    as (B, G, R, A) and emitting the default RGBA rawmode yields BGRA byte order.
    """
    from PIL import ImageChops
    img = img.convert("RGBA")
    r, g, b, a = img.split()
    r = ImageChops.multiply(r, a)
    g = ImageChops.multiply(g, a)
    b = ImageChops.multiply(b, a)
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


class LayeredOverlay:
    """A click-through, always-on-top, per-pixel-alpha Win32 layered window.

    All methods must be called from the single owner thread (the controller's
    anchor thread) — the window is created and painted there. Every Windows call
    is guarded; failures degrade to a hidden overlay, never an exception.
    """

    _CLASS = "RuneSyncOverlayWnd"

    def __init__(self):
        self._hwnd = None
        self._visible = False
        self._structs_ready = False

    # ctypes plumbing is built lazily so importing this module never touches
    # win32/GDI on a machine without them.
    def _ensure_structs(self):
        if self._structs_ready:
            return True
        import ctypes
        from ctypes import wintypes
        self._ct = ctypes
        self._user32 = ctypes.windll.user32
        self._gdi32 = ctypes.windll.gdi32

        class BMH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        class BMI(ctypes.Structure):
            _fields_ = [("bmiHeader", BMH), ("bmiColors", wintypes.DWORD * 3)]

        class BLEND(ctypes.Structure):
            _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                        ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]

        class PT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class SZ(ctypes.Structure):
            _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

        self._BMI, self._BLEND, self._PT, self._SZ = BMI, BLEND, PT, SZ

        # Handle-returning / handle-taking calls MUST declare pointer-sized
        # types or ctypes truncates them to 32-bit on x64 and crashes.
        vp = ctypes.c_void_p
        self._gdi32.CreateDIBSection.restype = vp
        self._gdi32.CreateDIBSection.argtypes = [vp, vp, wintypes.UINT,
                                                 ctypes.POINTER(vp), vp, wintypes.DWORD]
        self._gdi32.CreateCompatibleDC.restype = vp
        self._gdi32.CreateCompatibleDC.argtypes = [vp]
        self._gdi32.SelectObject.restype = vp
        self._gdi32.SelectObject.argtypes = [vp, vp]
        self._gdi32.DeleteObject.argtypes = [vp]
        self._gdi32.DeleteDC.argtypes = [vp]
        self._user32.GetDC.restype = vp
        self._user32.GetDC.argtypes = [vp]
        self._user32.ReleaseDC.argtypes = [vp, vp]
        self._user32.UpdateLayeredWindow.argtypes = [
            vp, vp, ctypes.POINTER(PT), ctypes.POINTER(SZ), vp,
            ctypes.POINTER(PT), wintypes.DWORD, ctypes.POINTER(BLEND), wintypes.DWORD]
        self._structs_ready = True
        return True

    def ensure(self) -> bool:
        if self._hwnd:
            return True
        if not (_HAVE_WIN32 and _HAVE_PIL):
            return False
        try:
            self._ensure_structs()
            hinst = win32gui.GetModuleHandle(None)
            try:
                wc = win32gui.WNDCLASS()
                wc.hInstance = hinst
                wc.lpszClassName = self._CLASS
                wc.lpfnWndProc = {}          # unhandled -> DefWindowProc
                win32gui.RegisterClass(wc)
            except Exception:
                pass                          # already registered
            ex = (win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT
                  | win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW
                  | win32con.WS_EX_NOACTIVATE)
            self._hwnd = win32gui.CreateWindowEx(
                ex, self._CLASS, "RuneSync Overlay", win32con.WS_POPUP,
                0, 0, 10, 10, 0, 0, hinst, None)
            win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)
            return True
        except Exception:
            self._hwnd = None
            return False

    def blit(self, img, x: int, y: int) -> bool:
        """Paint `img` (RGBA) at screen (x, y) with per-pixel alpha."""
        if not self._hwnd or not self._ensure_structs():
            return False
        ctypes = self._ct
        w, h = img.size
        buf = _to_premultiplied_bgra(img)
        screen_dc = self._user32.GetDC(None)
        mem_dc = self._gdi32.CreateCompatibleDC(screen_dc)
        hbmp = None
        old = None
        try:
            bmi = self._BMI()
            bmi.bmiHeader.biSize = ctypes.sizeof(self._BMI().bmiHeader)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h        # top-down
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0    # BI_RGB
            ppv = ctypes.c_void_p()
            hbmp = self._gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0,
                                                ctypes.byref(ppv), None, 0)
            if not hbmp or not ppv:
                return False
            ctypes.memmove(ppv, buf, len(buf))
            old = self._gdi32.SelectObject(mem_dc, hbmp)
            size = self._SZ(w, h)
            src = self._PT(0, 0)
            dst = self._PT(int(x), int(y))
            blend = self._BLEND(0, 0, 255, 1)   # AC_SRC_OVER, AC_SRC_ALPHA
            ok = self._user32.UpdateLayeredWindow(
                self._hwnd, screen_dc, ctypes.byref(dst), ctypes.byref(size),
                mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), 2)  # ULW_ALPHA
            return bool(ok)
        except Exception:
            return False
        finally:
            try:
                if old:
                    self._gdi32.SelectObject(mem_dc, old)
                if hbmp:
                    self._gdi32.DeleteObject(hbmp)
                self._gdi32.DeleteDC(mem_dc)
                self._user32.ReleaseDC(None, screen_dc)
            except Exception:
                pass

    def show(self):
        if self._visible or not self._hwnd:
            return
        try:
            win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)
            win32gui.SetWindowPos(
                self._hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
            self._visible = True
        except Exception:
            pass

    def hide(self):
        if not self._visible or not self._hwnd:
            return
        try:
            win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)
            self._visible = False
        except Exception:
            pass

    def destroy(self):
        if not self._hwnd:
            return
        try:
            win32gui.DestroyWindow(self._hwnd)
        except Exception:
            pass
        self._hwnd = None
        self._visible = False


class OverlayController:
    """Owns the anchor loop: track the client, render + position the overlay.

    `state_provider()` returns the compact champ-select snapshot to render (see
    bridge.Api.get_overlay_state). `should_show()` returns whether champ select
    is active (wired to the bridge's `in_champ_select` flag). All work is
    best-effort; an overlay hiccup never takes down monitoring.
    """

    def __init__(self, state_provider: Callable[[], dict],
                 should_show: Callable[[], bool]):
        self._state_provider = state_provider
        self._should_show = should_show
        self._overlay = LayeredOverlay()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="overlay-anchor")
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

    def _tick(self):
        if _HAVE_WIN32:
            try:
                win32gui.PumpWaitingMessages()
            except Exception:
                pass

        want = False
        try:
            want = bool(self._should_show())
        except Exception:
            want = False

        hwnd = find_client_window() if want else None
        if want and hwnd is None:
            want = False

        img = None
        if want:
            try:
                state = self._state_provider()
            except Exception:
                state = None
            theme = (state or {}).get("theme", "amber")
            img = render_panel(state, theme)
            want = img is not None

        if not want:
            self._overlay.hide()
            return

        if not self._overlay.ensure():
            return
        try:
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            self._overlay.hide()
            return

        sx, sy, sw, sh = _screen_bounds()
        w, h = img.size
        x, y = overlay_anchor(rect, w, h, sx, sy, sw, sh)
        if self._overlay.blit(img, x, y):
            self._overlay.show()
