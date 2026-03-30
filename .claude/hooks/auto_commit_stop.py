#!/usr/bin/env python3
"""Stop hook: commit and push everything staged by this Claude response.

Fires once when Claude finishes a full response. Bundles all file edits
from that response into a single commit so history stays readable.
Skips silently if nothing was staged.
"""
import subprocess
import sys


def main() -> None:
    # Check if anything is staged
    status = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        check=False,
    )
    if status.returncode == 0:
        return  # nothing staged, nothing to do

    result = subprocess.run(
        ["git", "commit", "-m", "auto: Claude edit via Claude Code"],
        check=False,
    )
    if result.returncode == 0:
        subprocess.run(["git", "push"], check=False)


if __name__ == "__main__":
    main()
