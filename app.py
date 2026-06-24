"""DAEMON — bootstrap for the pywebview UI.

Replaces main.py's __main__: single-instance mutex, logging, then a frameless
WebView2 window rendering webui/index.html. The Python<->JS bridge (bridge.py)
and live monitor wiring land in Phase 2; this is the shell.
"""
import sys, os, ctypes
import webview

_BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def resource_path(*parts) -> str:
    """Path into bundled resources, working both from source and PyInstaller onefile."""
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

    # Logging (owns the rotating file + the debug-console queue).
    import logging
    from log_setup import init_logging
    try:
        init_logging(os.path.join(_user_data_dir(), "runesync.log"))
    except Exception:
        pass
    logging.getLogger().info("DAEMON starting", extra={"rs_tag": "[app]", "rs_severity": "info"})

    index = resource_path("webui", "index.html")
    minimized = "--minimized" in sys.argv

    webview.create_window(
        "DAEMON",
        url=index,
        width=1066, height=768,
        resizable=False, frameless=True, easy_drag=False,
        background_color="#08070a",
        hidden=minimized,
    )
    # EdgeChromium user-data dir must be writable regardless of install location.
    webview.start(
        gui="edgechromium",
        storage_path=os.path.join(_user_data_dir(), "webview"),
        debug=("--devtools" in sys.argv),
    )


if __name__ == "__main__":
    main()
