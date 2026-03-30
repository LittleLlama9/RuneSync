#!/usr/bin/env python3
"""PostToolUse hook: stage the edited file for later commit.

The actual commit happens in auto_commit_stop.py (Stop hook), so all edits
from one Claude response land in a single commit rather than one per file.
"""
import json
import subprocess
import sys


def main() -> None:
    data = json.load(sys.stdin)
    fp: str = data.get("tool_input", {}).get("file_path", "")
    if fp:
        subprocess.run(["git", "add", fp], check=False)


if __name__ == "__main__":
    main()
