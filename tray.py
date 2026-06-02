"""
tray.py — system tray icon for RuneSync.

Replaces the two-process watcher.py model with a single in-process tray
icon that:
  - lives in the system tray for the app's full lifetime
  - hides the main window on close instead of quitting
  - polls for LeagueClientUx.exe and notifies the app when League starts
  - exposes a "Start with Windows" toggle that writes HKCU\\...\\Run

Public API:
    TrayController(
        on_show, on_quit, get_autostart, set_autostart,
        icon_path=...,
    ).start()

The tkinter mainloop owns the UI thread; the tray icon and the League
poller each run on their own daemon thread.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

try:
    import pystray
    from pystray import MenuItem as _Item, Menu as _Menu
    from PIL import Image
    _PYSTRAY_AVAILABLE = True
except Exception as _e:  # pystray not installed
    _PYSTRAY_AVAILABLE = False
    _IMPORT_ERROR = _e


LEAGUE_EXE = "LeagueClientUx.exe"
LEAGUE_POLL_INTERVAL = 5.0   # seconds


# ── Windows startup registry helpers ───────────────────────────────────────

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "RuneSync"


def _exe_path() -> str:
    """Path to RuneSync.exe (frozen) or the main.py script (dev)."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")


def is_autostart_enabled() -> bool:
    """True if RuneSync is registered to start with Windows."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, _RUN_VALUE)
            return bool(val)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    """Register or remove RuneSync from HKCU\\...\\Run. Returns success."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enabled:
                exe = _exe_path()
                # --minimized → RuneSync starts hidden in tray on Windows boot
                # instead of popping the window on every login.
                if not exe.lower().endswith(".exe"):
                    val = f'py "{exe}" --minimized'
                else:
                    val = f'"{exe}" --minimized'
                winreg.SetValueEx(k, _RUN_VALUE, 0, winreg.REG_SZ, val)
            else:
                try:
                    winreg.DeleteValue(k, _RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        print(f"[tray] autostart toggle failed: {e}", file=sys.stderr)
        return False


# ── League poller ──────────────────────────────────────────────────────────

def _league_is_running() -> bool:
    """True if LeagueClientUx.exe is in the process list."""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {LEAGUE_EXE}", "/NH"],
            capture_output=True, text=True,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            timeout=5,
        )
        return LEAGUE_EXE.lower() in out.stdout.lower()
    except Exception:
        return False


class LeaguePoller:
    """Polls for League and calls on_open() / on_close() on transitions."""

    def __init__(self, on_open: Callable[[], None],
                 on_close: Optional[Callable[[], None]] = None,
                 interval: float = LEAGUE_POLL_INTERVAL):
        self.on_open = on_open
        self.on_close = on_close or (lambda: None)
        self.interval = interval
        self._stop = threading.Event()
        self._was_running = False

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            running = _league_is_running()
            if running and not self._was_running:
                try:
                    self.on_open()
                except Exception as e:
                    print(f"[tray] on_open error: {e}", file=sys.stderr)
            elif not running and self._was_running:
                try:
                    self.on_close()
                except Exception as e:
                    print(f"[tray] on_close error: {e}", file=sys.stderr)
            self._was_running = running
            self._stop.wait(self.interval)


# ── Tray controller ────────────────────────────────────────────────────────

class TrayController:
    """
    on_show:        called when user clicks "Show RuneSync" (or double-clicks tray)
    on_quit:        called when user clicks "Quit RuneSync" — should exit the app
    get_autostart:  return current autostart bool (usually is_autostart_enabled)
    set_autostart:  set autostart bool (usually module-level set_autostart)
    icon_path:      path to icon.ico
    """

    def __init__(self,
                 on_show: Callable[[], None],
                 on_quit: Callable[[], None],
                 get_autostart: Callable[[], bool] = is_autostart_enabled,
                 set_autostart: Callable[[bool], bool] = set_autostart,
                 icon_path: Optional[str] = None):
        self.on_show = on_show
        self.on_quit = on_quit
        self.get_autostart = get_autostart
        self.set_autostart = set_autostart
        self.icon_path = icon_path
        self._icon = None  # pystray.Icon

    def available(self) -> bool:
        return _PYSTRAY_AVAILABLE and sys.platform == "win32"

    def start(self) -> None:
        if not self.available():
            return
        image = self._load_image()
        self._icon = pystray.Icon("RuneSync", image, "RuneSync", menu=self._menu())
        threading.Thread(target=self._icon.run, daemon=True).start()

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass

    def notify(self, title: str, message: str) -> None:
        if self._icon:
            try:
                self._icon.notify(message, title)
            except Exception:
                pass

    # ── internals ──────────────────────────────────────────────────────────

    def _load_image(self):
        if self.icon_path and os.path.exists(self.icon_path):
            try:
                return Image.open(self.icon_path)
            except Exception:
                pass
        # Fallback: 64x64 magenta square so the icon is obviously visible.
        return Image.new("RGBA", (64, 64), (200, 32, 200, 255))

    def _menu(self):
        return _Menu(
            _Item("Show RuneSync", lambda i, _it: self._safe(self.on_show), default=True),
            _Menu.SEPARATOR,
            _Item("Start with Windows", self._on_autostart_toggle,
                  checked=lambda _it: self.get_autostart()),
            _Menu.SEPARATOR,
            _Item("Quit RuneSync", lambda i, _it: self._safe(self.on_quit)),
        )

    def _on_autostart_toggle(self, icon, item):
        new = not self.get_autostart()
        self.set_autostart(new)

    @staticmethod
    def _safe(fn):
        try:
            fn()
        except Exception as e:
            print(f"[tray] callback error: {e}", file=sys.stderr)
