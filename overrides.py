"""
OverrideManager — stores per-champion custom rune overrides and app settings.
Saved to %APPDATA%/RuneSync/overrides.json
"""

import os
import json
from pathlib import Path
from typing import Optional


def _config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "RuneSync"
    d.mkdir(parents=True, exist_ok=True)
    return d


class OverrideManager:
    def __init__(self):
        self._path = _config_dir() / "overrides.json"
        self._data: dict = self._load()

    def _load(self) -> dict:
        data: dict = {"overrides": {}, "settings": {}}
        if self._path.exists():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data.update(loaded)
            except Exception:
                pass
        # A hand-edited or partially-written file can parse yet be missing a key
        # (or have it as null/non-dict); get()/set() index these directly, so
        # normalize before returning rather than crashing on first access.
        if not isinstance(data.get("overrides"), dict):
            data["overrides"] = {}
        if not isinstance(data.get("settings"), dict):
            data["settings"] = {}
        return data

    def _save(self):
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    def get(self, champion: str) -> Optional[dict]:
        key = champion.lower().strip()
        return self._data["overrides"].get(key)

    def has(self, champion: str) -> bool:
        return self.get(champion) is not None

    def set(self, champion: str, data: dict):
        key = champion.lower().strip()
        self._data["overrides"][key] = data
        self._save()

    def remove(self, champion: str):
        key = champion.lower().strip()
        self._data["overrides"].pop(key, None)
        self._save()
    def all(self) -> dict:
        return {k.title(): v for k, v in self._data["overrides"].items()}

    @property
    def settings(self) -> dict:
        return self._data.get("settings", {})

    def save_settings(self, settings: dict):
        self._data["settings"] = settings
        self._save()
