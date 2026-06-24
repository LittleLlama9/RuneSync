"""DAEMON — bootstrap for the pywebview UI.

Replaces main.py's __main__: single-instance mutex, logging, the bridge (Api +
Pusher), the frameless WebView2 window, tray + League auto-detect, and the
hide-to-tray-on-close lifecycle. All League logic stays in monitor/lcu/ugg.
"""
import sys, os, ctypes
import webview

from bridge import Api, Pusher
from tray import TrayController, LeaguePoller

_BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def resource_path(*parts) -> str:
    return os.path.join(_BASE_DIR, *parts)


def _user_data_dir() -> str:
    """%APPDATA%/RuneSync — writable on any install (including Program Files)."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "RuneSync")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        import tempfile
        d = tempfile.gettempdir()
    return d


def main():
    # Single-instance guard — identical contract to the Tk app.
    ctypes.windll.kernel32.CreateMutexW(None, False, "RuneSyncSingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)

    import logging
    from log_setup import init_logging
    log_queue = None
    try:
        log_queue = init_logging(os.path.join(_user_data_dir(), "runesync.log"))
    except Exception:
        pass
    logging.getLogger().info("DAEMON starting", extra={"rs_tag": "[app]", "rs_severity": "info"})

    minimized = "--minimized" in sys.argv
    pusher = Pusher()
    api = Api(pusher)
    api.log_queue = log_queue   # feeds the debug console drain

    window = webview.create_window(
        "DAEMON",
        url=resource_path("webui", "index.html"),
        js_api=api,
        width=1066, height=768,
        resizable=False, frameless=True, easy_drag=False,
        background_color="#08070a",
        hidden=minimized,
    )

    # tray + League auto-detect (callbacks run on their own daemon threads;
    # pywebview window methods are thread-safe).
    def _show():
        try:
            window.show()
        except Exception:
            pass

    tray = TrayController(on_show=_show, on_quit=api.quit_app,
                          icon_path=resource_path("icon.ico"))
    poller = LeaguePoller(on_open=api.on_league_open, on_close=api.on_league_close)
    api.tray = tray
    api.poller = poller

    # X button → hide to tray instead of quitting (returning False cancels close).
    def _on_closing():
        if api._quitting:
            return True
        api.hide_to_tray()
        return False
    window.events.closing += _on_closing

    def _on_start():
        tray.start()
        poller.start()
        api.boot()

    webview.start(
        _on_start,
        gui="edgechromium",
        storage_path=os.path.join(_user_data_dir(), "webview"),
        debug=("--devtools" in sys.argv),
    )


if __name__ == "__main__":
    main()
