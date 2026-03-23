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

def main():
    # Launch the scraping server as a hidden background process.
    # It runs for the entire Windows session regardless of whether League is open.
    _server_proc = subprocess.Popen(
        ["py", "-m", "uvicorn", "main:app", "--host", "0.0.0.0",
         "--port", "8000", "--no-access-log"],
        cwd=r"C:\Users\Matth\RuneSyncServer",
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )
    print("RuneSync Server started.", flush=True)

    runesync_proc = None
    print("RuneSync Watcher started — waiting for League client...")

    while True:
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
