"""
RuneSync Watcher — launches RuneSync when League opens, closes it when League closes.
This is what runs at startup instead of main.py directly.
"""

# ── DEV MODE — auto-launch disabled ───────────────────────────────────────
# Set to False once the server-based version is fully tested and ready.
DEV_MODE = False
if DEV_MODE:
    import sys as _sys
    print("RuneSync Watcher: DEV_MODE is ON — auto-launch disabled.")
    print("Use RuneSync_backup_2026-03-16 for the stable auto-launching version.")
    _sys.exit(0)
# ──────────────────────────────────────────────────────────────────────────

import subprocess, time, sys, os

LEAGUE_EXE   = "LeagueClientUx.exe"
# When compiled by PyInstaller, sys.executable is the watcher exe itself (in dist\).
# When run as a script, __file__ gives us the source dir.
import sys as _sys
_is_frozen = getattr(_sys, "frozen", False)
_base = os.path.dirname(_sys.executable) if _is_frozen else os.path.dirname(os.path.abspath(__file__))
_exe = os.path.join(_base, "RuneSync.exe")                          # sibling in dist\ when frozen
_exe_dist = os.path.join(_base, "dist", "RuneSync.exe")             # dist\ when run as script
_script = os.path.join(_base, "main.py") if not _is_frozen else None
if os.path.exists(_exe):
    RUNESYNC_CMD = [_exe]
elif os.path.exists(_exe_dist):
    RUNESYNC_CMD = [_exe_dist]
elif _script:
    RUNESYNC_CMD = ["py", _script]
else:
    RUNESYNC_CMD = None
CHECK_INTERVAL = 5  # seconds

def is_league_running():
    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {LEAGUE_EXE}", "/NH"],
        capture_output=True, text=True, creationflags=0x08000000
    )
    return LEAGUE_EXE.lower() in result.stdout.lower()

def is_runesync_running(proc):
    return proc is not None and proc.poll() is None

SERVER_CMD = ["py", "-m", "uvicorn", "main:app", "--host", "0.0.0.0",
              "--port", "8000", "--no-access-log"]
SERVER_CWD  = os.path.join(_base, "server") if not _is_frozen else os.path.join(_base, "server")

_server_log = None

def _open_server_log():
    """Open (append) the server log file, return file handle or None."""
    try:
        log_path = os.path.join(SERVER_CWD, "server.log")
        return open(log_path, "a", encoding="utf-8", buffering=1)
    except Exception:
        return None

def start_server():
    global _server_log
    try:
        if _server_log is None:
            _server_log = _open_server_log()
        proc = subprocess.Popen(
            SERVER_CMD,
            cwd=SERVER_CWD,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            stdout=_server_log,
            stderr=_server_log,
        )
        print("RuneSync Server started.", flush=True)
        return proc
    except Exception as e:
        print(f"ERROR: Could not start RuneSync Server: {e}", flush=True)
        return None

def main():
    server_proc = start_server()

    runesync_proc = None
    print("RuneSync Watcher started — waiting for League client...")

    while True:
        # Restart server if it crashed
        if server_proc is None or server_proc.poll() is not None:
            print("RuneSync Server stopped — restarting...", flush=True)
            server_proc = start_server()

        league_up = is_league_running()

        if league_up and not is_runesync_running(runesync_proc):
            if not RUNESYNC_CMD:
                print("ERROR: Cannot find RuneSync.exe or main.py!", flush=True)
            else:
                print("League detected — launching RuneSync...")
                runesync_proc = subprocess.Popen(RUNESYNC_CMD)

        elif not league_up and is_runesync_running(runesync_proc):
            print("League closed — shutting down RuneSync...")
            runesync_proc.terminate()
            try:
                runesync_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                runesync_proc.kill()
            runesync_proc = None

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
