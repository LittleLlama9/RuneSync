#!/usr/bin/env python3
"""Stop hook: commit and push everything staged by this Claude response.

Fires once when Claude finishes a full response. Reads .git/CLAUDE_COMMIT_MSG
for a custom message if Claude asked the user for one; falls back to the
next auto-version (e.g. v1.01, v1.02) if not. Skips silently if nothing
was staged.

Version is stored in VERSION at the repo root and committed alongside the
code changes.
"""
import os
import subprocess


MSG_FILE = os.path.join(".git", "CLAUDE_COMMIT_MSG")
VERSION_FILE = "VERSION"


def _read_version() -> tuple[int, int]:
    """Return (major, minor) parsed from VERSION file, defaulting to (1, 0)."""
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            text = f.read().strip()
        major_s, minor_s = text.split(".")
        return int(major_s), int(minor_s)
    except Exception:
        return 1, 0


def _write_version(major: int, minor: int) -> None:
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write(f"{major}.{minor:02d}\n")


def _next_version_msg() -> str:
    """Increment minor version, persist it, stage the VERSION file, return label."""
    major, minor = _read_version()
    minor += 1
    _write_version(major, minor)
    subprocess.run(["git", "add", VERSION_FILE], check=False)
    return f"v{major}.{minor:02d}"


def main() -> None:
    # Skip if nothing is staged
    status = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
    if status.returncode == 0:
        return

    # Use custom message if Claude wrote one, otherwise auto-version
    msg = None
    if os.path.exists(MSG_FILE):
        with open(MSG_FILE, encoding="utf-8") as f:
            custom = f.read().strip()
        os.remove(MSG_FILE)
        if custom:
            msg = custom

    if msg is None:
        msg = _next_version_msg()

    result = subprocess.run(["git", "commit", "-m", msg], check=False)
    if result.returncode == 0:
        subprocess.run(["git", "push"], check=False)


if __name__ == "__main__":
    main()
