"""overlay.py — champ-select overlay window anchored to the League client.

A second, frameless, always-on-top pywebview window that docks to the edge of
the League client and shows the champ-select panels (matchup win rate, counter
picks, draft/composition read) we already compute. It is:

  * read-only — it renders data the monitor already produces; it never drives
    picks, bans, or any client action, so it stays within Riot's policy.
  * opt-in to visibility — shown only while the user is in champ select and the
    League client window is actually on screen, hidden otherwise.
  * defensive — every Windows/window call is guarded; if the League window can't
    be located (or win32 isn't available) the overlay simply stays hidden and
    the rest of the app is unaffected.

Windowing note: WebView2 (our renderer) does not support a transparent window,
so this is an opaque panel docked to the client edge rather than a see-through
overlay painted over the champ-select art.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

try:
    import win32gui  # type: ignore
    import win32con  # type: ignore
    _HAVE_WIN32 = True
except Exception:  # pragma: no cover - non-Windows / missing pywin32
    _HAVE_WIN32 = False

# The League client (lobby / champ select / post-game) renders in a window whose
# class is "RCLIENT". The in-game process uses a different class, so matching on
# RCLIENT keeps the overlay tied to the client where champ select actually lives.
_CLIENT_WINDOW_CLASS = "RCLIENT"
_CLIENT_WINDOW_TITLE = "League of Legends"

PANEL_WIDTH = 300
_MIN_PANEL_HEIGHT = 240
_POLL_INTERVAL = 0.5


def overlay_geometry(client_rect, screen_w: int, screen_h: int,
                     panel_w: int = PANEL_WIDTH,
                     screen_x: int = 0, screen_y: int = 0):
    """Return (x, y, w, h) for the overlay docked to the League client.

    Prefers docking just to the RIGHT of the client so it never covers the
    champ-select UI. If there isn't room to the right (client near the screen
    edge / maximised), it tucks against the client's inner-right edge instead.
    The panel height matches the client; both are clamped to the screen so the
    window is always fully visible and reachable.

    `client_rect` is (left, top, right, bottom) in screen pixels. `screen_x/y`
    are the origin of the (virtual) desktop; monitors left of / above the
    primary have negative origins, so clamping is done within
    [screen_x, screen_x+screen_w] × [screen_y, screen_y+screen_h] rather than
    assuming a (0, 0) origin.
    """
    left, top, right, bottom = client_rect
    sx0, sy0 = screen_x, screen_y
    sx1, sy1 = screen_x + screen_w, screen_y + screen_h

    ch = max(_MIN_PANEL_HEIGHT, bottom - top)
    ch = min(ch, screen_h)

    y = top
    if y + ch > sy1:               # keep the bottom on screen
        y = max(sy0, sy1 - ch)
    if y < sy0:
        y = sy0

    # Room to the right of the client?
    if right + panel_w <= sx1:
        x = right
    else:
        # Dock against the client's inner right edge (overlapping the client).
        x = max(sx0, right - panel_w)
        if x + panel_w > sx1:
            x = max(sx0, sx1 - panel_w)

    return x, y, panel_w, ch


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


class OverlayController:
    """Owns the anchor loop: track the client, show/hide + position the overlay.

    `should_show()` is a zero-arg callable returning whether champ select is
    active (wired to the bridge's `in_champ_select` flag). `window` is the
    pywebview overlay window (created hidden). All window operations are
    best-effort; failures never propagate.
    """

    def __init__(self, window, should_show):
        self._window = window
        self._should_show = should_show
        self._visible = False
        self._last_geom = None
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
                # An overlay hiccup must never take down monitoring.
                pass

    def _tick(self):
        want = False
        hwnd = None
        try:
            want = bool(self._should_show())
        except Exception:
            want = False

        if want:
            hwnd = find_client_window()
            # If the client window is gone the overlay can't be anchored — hide.
            want = hwnd is not None

        if not want:
            self._hide()
            return

        try:
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            self._hide()
            return

        sx, sy, sw, sh = _screen_bounds()
        geom = overlay_geometry(rect, sw, sh, screen_x=sx, screen_y=sy)
        self._position(geom)
        self._show()

    def _position(self, geom):
        if geom == self._last_geom:
            return
        x, y, w, h = geom
        try:
            self._window.resize(w, h)
            self._window.move(x, y)
            self._last_geom = geom
        except Exception:
            pass

    def _show(self):
        if self._visible:
            return
        try:
            self._window.show()
            self._visible = True
        except Exception:
            pass

    def _hide(self):
        if not self._visible:
            return
        try:
            self._window.hide()
            self._visible = False
        except Exception:
            pass
