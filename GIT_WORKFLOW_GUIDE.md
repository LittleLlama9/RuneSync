# Git / GitHub / VS Code / Claude Code Workflow — Setup Checklist

Copy this file into any new project. Work through each section top to bottom.
Everything here matches the setup in RuneSync (the reference implementation).

---

## 1 — Git repo + initial push

```bash
git init
git add .
git commit -m "v1.00 — init"
# Create repo on GitHub (no README, no .gitignore — you already have one)
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git branch -M main
git push -u origin main
```

---

## 2 — .gitignore

Create `.gitignore` at the repo root. Minimum contents:

```
# Secrets
.env
*.env

# Python
__pycache__/
*.pyc
dist/
build/

# Claude Code local settings (machine-specific, never commit)
.claude/settings.local.json

# Test artifacts
.pytest_cache/
.coverage
htmlcov/

# OS
.DS_Store
Thumbs.db
```

---

## 3 — Pre-commit hook (blocks bad commits)

Create `.git/hooks/pre-commit` — this file lives inside `.git/` so it is
**not tracked by git** and must be re-created on each fresh clone.

```bash
#!/usr/bin/env bash
set -euo pipefail

# Block .env files
if git diff --cached --name-only | grep -qE '(^|/)(\.env|.*\.env)$'; then
    echo "ERROR: .env file staged — refusing commit to protect secrets."
    exit 1
fi

# Syntax-check staged Python files
fail=0
staged_py=$(git diff --cached --name-only --diff-filter=ACM | grep '\.py$' || true)
if [ -n "$staged_py" ]; then
    while IFS= read -r file; do
        if ! python -m py_compile "$file" 2>/tmp/py_compile_err; then
            echo "ERROR: Syntax error in $file:"
            cat /tmp/py_compile_err
            fail=1
        fi
    done <<< "$staged_py"
    [ "$fail" -eq 0 ] || exit 1
fi

# Run tests
if [ -d "tests" ]; then
    python -m pytest tests/ -q --tb=short
fi
```

Make it executable:

```bash
chmod +x .git/hooks/pre-commit
```

> **On Windows (Git Bash):** `chmod` works fine. On fresh clones, re-run `chmod +x`.

---

## 4 — requirements.txt + pytest

Create `requirements.txt`:

```
pytest
# add your project's dependencies below
```

Create `pytest.ini`:

```ini
[pytest]
pythonpath = .
testpaths = tests
```

Create a `tests/` folder with at minimum:

**`tests/test_imports.py`** — verifies every source file parses without syntax errors
and core modules import cleanly. Adjust `IMPORTABLE` to match your project's modules.

```python
import ast
import pathlib
import importlib
import pytest

ROOT = pathlib.Path(__file__).parent.parent
ALL_PY = sorted(
    p for p in ROOT.glob("*.py")
    if "__pycache__" not in str(p)
    and not (p.name.startswith("_") and not p.name.startswith("__"))
)

@pytest.mark.parametrize("path", ALL_PY, ids=lambda p: p.name)
def test_all_source_files_parse(path):
    ast.parse(path.read_text(encoding="utf-8"))

IMPORTABLE = ["log_setup", "your_module", "another_module"]  # adjust

@pytest.mark.parametrize("mod", IMPORTABLE)
def test_module_imports(mod):
    importlib.import_module(mod)
```

---

## 5 — GitHub Actions CI

Create `.github/workflows/tests.yml`:

```yaml
name: Tests

on:
  push:
    branches: ["**"]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.x"
          cache: pip

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run tests
        run: python -m pytest tests/ -v
```

> If your project uses tkinter, add this step before "Install dependencies":
> ```yaml
> - name: Install tkinter
>   run: sudo apt-get install -y python3-tk
> ```

Push to GitHub — the Actions tab will show a green check on passing commits.

---

## 6 — Claude Code commit naming

These files let Claude prompt you for a commit name before making changes,
and fall back to auto-versioning (v1.01, v1.02 …) if you skip.

### 6a — Directory structure

```
.claude/
  hooks/
    auto_commit_stop.py   ← run manually after edits to commit + push
```

### 6b — `auto_commit_stop.py`

```python
#!/usr/bin/env python3
"""Commit all staged files. Falls back to auto-versioning when no named commit
was set. Run this manually after staging your changes:
  git add <files> && python .claude/hooks/auto_commit_stop.py
"""
import os
import subprocess

MSG_FILE = os.path.join(".git", "CLAUDE_COMMIT_MSG")
VERSION_FILE = "VERSION"

def _read_version() -> tuple[int, int]:
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
    major, minor = _read_version()
    minor += 1
    _write_version(major, minor)
    subprocess.run(["git", "add", VERSION_FILE], check=False)
    return f"v{major}.{minor:02d}"

def main() -> None:
    status = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
    if status.returncode == 0:
        return

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
```

### 6c — CLAUDE.md commit naming instruction

Add this to your `CLAUDE.md` so Claude always prompts for a commit name:

```markdown
COMMIT NAMING:
Before making any code changes, use AskUserQuestion to ask:
  "What should I name this commit? (press Enter to skip)"
If the user provides a name, write it to .git/CLAUDE_COMMIT_MSG. After all edits
are done, stage the changed files and run:
  python .claude/hooks/auto_commit_stop.py
Version is tracked in the VERSION file (e.g. 1.00, 1.01 …). The script increments
the minor version automatically when no named message is given.
Skip this prompt for trivial follow-up fixes within the same conversation.
```

---

## 7 — VERSION file

Create `VERSION` at the repo root:

```
1.00
```

Commit it. From here on, every unnamed commit auto-increments this
(v1.01, v1.02 …). To bump the major version (e.g. v2.00), edit the file manually.

---

## 8 — Branch workflow

```
main = always stable and deployable

For any experiment or potentially-breaking feature:
  git checkout -b feature/short-description

Work normally. When it's working:
  git checkout main && git merge feature/short-description

If it doesn't work:
  git branch -D feature/short-description

Never force-push main.
Never skip the pre-commit hook (--no-verify).
```

---

## Quick-start checklist for Claude

Paste this into a new Claude Code session to set everything up:

```
Please set up the following git/GitHub/Claude Code workflow for this project.
Reference: RuneSync repo (ask me for the guide file if needed).

[ ] 1. .gitignore — secrets, __pycache__, dist, build, .claude/settings.local.json
[ ] 2. Pre-commit hook at .git/hooks/pre-commit — blocks .env, syntax-checks staged
       .py files, runs pytest. chmod +x after creating.
[ ] 3. requirements.txt (at minimum: pytest) + pytest.ini (pythonpath=., testpaths=tests)
[ ] 4. tests/test_imports.py — ast.parse all root .py files + import core modules
[ ] 5. .github/workflows/tests.yml — CI on push/PR, ubuntu-latest, pip cache, pytest -v
[ ] 6. .claude/hooks/auto_commit_stop.py — commit + push with auto-version fallback,
       reads .git/CLAUDE_COMMIT_MSG for named commits
[ ] 7. VERSION file at repo root containing "1.00"
[ ] 8. CLAUDE.md — add COMMIT NAMING section: use AskUserQuestion before edits,
       write answer to .git/CLAUDE_COMMIT_MSG, run auto_commit_stop.py after staging
[ ] 9. Initial push to GitHub with all of the above committed
[ ] 10. Confirm GitHub Actions shows green on first push
```
