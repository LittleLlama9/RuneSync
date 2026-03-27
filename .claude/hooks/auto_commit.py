#!/usr/bin/env python3
"""PostToolUse hook: auto-stage + commit + push when Claude writes or edits a file."""
import json
import os
import subprocess
import sys


def main() -> None:
    data = json.load(sys.stdin)
    fp: str = data.get("tool_input", {}).get("file_path", "")
    if not fp:
        return

    # git add works with absolute or relative paths
    subprocess.run(["git", "add", fp], check=False)

    result = subprocess.run(
        ["git", "commit", "-m", "auto: Claude edit via Claude Code"],
        check=False,
    )
    if result.returncode == 0:
        subprocess.run(["git", "push"], check=False)


if __name__ == "__main__":
    main()
