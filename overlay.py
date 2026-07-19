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
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
    _HAVE_PIL = True
except Exception:  # pragma: no cover
    _HAVE_PIL = False

# The League client (lobby / champ select / post-game) renders in a window whose
# class is "RCLIENT". The in-game process uses a different class, so matching on
# RCLIENT keeps the overlay tied to the client where champ select actually lives.
_CLIENT_WINDOW_CLASS = "RCLIENT"
_CLIENT_WINDOW_TITLE = "League of Legends"

PANEL_WIDTH = 300
_MARGIN = 16
_TOP_OFFSET = 68           # clear the client's top bar
_POLL_INTERVAL = 0.5

# ── theme ────────────────────────────────────────────────────────────────────
_THEMES = {
    "amber":   (255, 176, 0),
    "green":   (74, 255, 145),
    "ice":     (120, 200, 255),
    "magenta": (255, 92, 205),
    "red":     (255, 96, 96),
}
_BG = (9, 12, 18, 214)         # semi-transparent panel fill
_BG_HEAD = (14, 19, 28, 232)   # slightly denser header band
_MUTED = (150, 162, 178)
_TEXT = (222, 230, 240)
_WARN = (255, 138, 96)
_GOOD = (86, 224, 140)


def _asset(*parts) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


_FONTS: dict = {}


def _font(bold: bool, size: int):
    key = (bold, size)
    f = _FONTS.get(key)
    if f is None:
        name = "SpaceMono-Bold.ttf" if bold else "SpaceMono-Regular.ttf"
        try:
            f = ImageFont.truetype(_asset("webui", "fonts", name), size)
        except Exception:
            try:
                f = ImageFont.load_default()
            except Exception:
                f = None
        _FONTS[key] = f
    return f


def _accent(theme: str):
    return _THEMES.get((theme or "amber").lower(), _THEMES["amber"])


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
    select so the user can see the overlay is live."""
    if not state or not _HAVE_PIL:
        return None

    accent = _accent(theme or state.get("theme", "amber"))
    pad = 14
    inner_w = width - pad * 2

    f_head = _font(True, 15)
    f_tag = _font(True, 10)
    f_label = _font(True, 11)
    f_body = _font(False, 13)
    f_small = _font(False, 11)

    def _line_h(font, extra=4):
        try:
            a, d = font.getmetrics()
            return a + d + extra
        except Exception:
            return (getattr(font, "size", 12) or 12) + extra

    # Two-pass layout: build a flat item list + accumulate height, then draw.
    items = []
    y = pad

    items.append(("head", ("RUNESYNC", "CHAMP SELECT")))
    y += _line_h(f_head, 10)

    mu = _has_matchup(state)
    co = _counters_of(state)
    dr = _draft_of(state)

    if mu:
        items.append(("rule", None)); y += 10
        champ = state.get("champ") or "You"
        enemy = state.get("enemy") or "?"
        items.append(("section", "MATCHUP")); y += _line_h(f_label, 5)
        items.append(("mvs", (champ, enemy))); y += _line_h(f_body, 3)
        wr = state.get("wr")
        lab = (state.get("wrLabel") or "").strip()
        tag = state.get("wrTag") or "info"
        wr_txt = (f"{wr:.1f}% WR" if isinstance(wr, (int, float)) else "WR —")
        if lab:
            wr_txt += f"  ·  {lab}"
        items.append(("wr", (wr_txt, tag))); y += _line_h(f_body, 6)

    if co:
        items.append(("rule", None)); y += 10
        items.append(("section", f"COUNTERS vs {co.get('enemy', '')}".strip()))
        y += _line_h(f_label, 5)
        for row in (co.get("counters") or [])[:4]:
            items.append(("counter", row)); y += _line_h(f_small, 4)

    if dr:
        items.append(("rule", None)); y += 10
        items.append(("section", "DRAFT")); y += _line_h(f_label, 5)
        for ob in (dr.get("observations") or [])[:4]:
            txt = ob.get("text") if isinstance(ob, dict) else str(ob)
            items.append(("obs", txt)); y += _line_h(f_small, 4)

    if not (mu or co or dr):
        items.append(("hint", "Reading lobby…")); y += _line_h(f_small, 4)

    height = y + pad
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    radius = 12
    d.rounded_rectangle([0, 0, width - 1, height - 1], radius=radius, fill=_BG)
    d.rounded_rectangle([0, 0, width - 1, 34], radius=radius, fill=_BG_HEAD)
    d.rectangle([0, 24, width - 1, 34], fill=_BG_HEAD)
    d.line([0, 35, width - 1, 35], fill=accent + (90,), width=1)
    d.rectangle([0, 0, 3, height - 1], fill=accent + (255,))  # left accent bar

    cy = pad
    for kind, payload in items:
        if kind == "head":
            title, tag = payload
            d.text((pad, cy), title, font=f_head, fill=accent + (255,))
            if f_tag is not None:
                tw = d.textlength(tag, font=f_tag)
                d.text((width - pad - tw, cy + 4), tag, font=f_tag, fill=_MUTED + (255,))
            cy += _line_h(f_head, 10)
        elif kind == "rule":
            d.line([pad, cy, width - pad, cy], fill=(255, 255, 255, 26), width=1)
            cy += 10
        elif kind == "section":
            d.text((pad, cy), _fit(d, payload, f_label, inner_w),
                   font=f_label, fill=accent + (220,))
            cy += _line_h(f_label, 5)
        elif kind == "mvs":
            champ, enemy = payload
            txt = _fit(d, f"{champ}  vs  {enemy}", f_body, inner_w)
            d.text((pad, cy), txt, font=f_body, fill=_TEXT + (255,))
            cy += _line_h(f_body, 3)
        elif kind == "wr":
            txt, tag = payload
            col = _GOOD if tag == "success" else _WARN if tag in ("warn", "error") else _TEXT
            d.text((pad, cy), _fit(d, txt, f_body, inner_w), font=f_body, fill=col + (255,))
            cy += _line_h(f_body, 6)
        elif kind == "counter":
            row = payload
            name = row.get("champion", "") if isinstance(row, dict) else str(row)
            wr = row.get("win_rate") if isinstance(row, dict) else None
            wr_s = f"{wr:.1f}%" if isinstance(wr, (int, float)) else ""
            name = _fit(d, name, f_small, inner_w - 46)
            d.text((pad + 4, cy), name, font=f_small, fill=_TEXT + (255,))
            if wr_s:
                tw = d.textlength(wr_s, font=f_small)
                d.text((width - pad - tw, cy), wr_s, font=f_small, fill=_GOOD + (255,))
            cy += _line_h(f_small, 4)
        elif kind == "obs":
            d.text((pad + 4, cy), _fit(d, "• " + (payload or ""), f_small, inner_w - 4),
                   font=f_small, fill=_MUTED + (255,))
            cy += _line_h(f_small, 4)
        elif kind == "hint":
            d.text((pad, cy), payload, font=f_small, fill=_MUTED + (255,))
            cy += _line_h(f_small, 4)

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
