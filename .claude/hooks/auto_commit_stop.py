#!/usr/bin/env python3
"""Stop hook: commit and push everything staged by this Claude response.

Fires once when Claude finishes a full response. Reads .git/CLAUDE_COMMIT_MSG
for a custom message if Claude asked the user for one; falls back to the
generic message if not. Skips silently if nothing was staged.
"""
import os
import subprocess


MSG_FILE = os.path.join(".git", "CLAUDE_COMMIT_MSG")
FALLBACK = "auto: Claude edit via Claude Code"


def main() -> None:
    # Skip if nothing is staged
    status = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
    if status.returncode == 0:
        return

    # Use custom message if Claude wrote one, otherwise fall back
    msg = FALLBACK
    if os.path.exists(MSG_FILE):
        with open(MSG_FILE, encoding="utf-8") as f:
            custom = f.read().strip()
        os.remove(MSG_FILE)
        if custom:
            msg = custom

    result = subprocess.run(["git", "commit", "-m", msg], check=False)
    if result.returncode == 0:
        subprocess.run(["git", "push"], check=False)


if __name__ == "__main__":
    main()
