"""
test_imports.py — Verify all source .py files parse and core modules import cleanly.

Syntax check covers every .py in the project root.
Import check covers non-GUI modules (main.py is excluded — it calls tk.Tk() at
module level outside any guard, so it cannot be imported safely in a test runner).
"""
import ast
import importlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent.parent

# All .py files in the project root.
# Exclude single-underscore-prefixed files (e.g. _matchup_method.py) — these
# are code snippets that are not standalone modules and may contain
# intentionally partial/indented code.
ALL_PY = sorted(
    p for p in ROOT.glob("*.py")
    if "__pycache__" not in str(p)
    and not (p.name.startswith("_") and not p.name.startswith("__"))
)

# Modules safe to import: no blocking startup code, no heavy GUI deps.
# app.py / bridge.py are parse-checked only (they import pywebview, which may be
# absent in a headless runner); the UI now lives in webui/ + app.py + bridge.py.
IMPORTABLE = [
    "log_setup",
    "ugg_api",
    "lcu",
    "overrides",
    "champion_roles",
    "monitor",
    "item_data",
    "perks",
    "tray",
]


def test_all_source_files_parse():
    """Every .py in the project root must be valid Python (ast.parse)."""
    errors = []
    for path in ALL_PY:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path.name}:{exc.lineno}: {exc.msg}")
    assert not errors, "Syntax errors found:\n" + "\n".join(errors)


def test_core_modules_import():
    """Core non-GUI modules must be importable without raising ImportError."""
    sys.path.insert(0, str(ROOT))
    errors = []
    for mod in IMPORTABLE:
        try:
            importlib.import_module(mod)
        except ImportError as exc:
            errors.append(f"{mod}: {exc}")
    assert not errors, "ImportError(s):\n" + "\n".join(errors)
