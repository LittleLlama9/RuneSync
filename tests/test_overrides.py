"""
test_overrides.py — OverrideManager tolerates malformed/partial settings files.

A hand-edited or partially-written overrides.json must never crash startup.
"""
import json

import overrides
from overrides import OverrideManager


def _mgr(tmp_path, monkeypatch, raw=None):
    monkeypatch.setattr(overrides, "_config_dir", lambda: tmp_path)
    if raw is not None:
        (tmp_path / "overrides.json").write_text(raw, encoding="utf-8")
    return OverrideManager()


def test_missing_file_defaults(tmp_path, monkeypatch):
    m = _mgr(tmp_path, monkeypatch)
    assert m.all() == {}
    assert m.settings == {}
    assert m.has("Garen") is False


def test_partial_file_missing_overrides_key(tmp_path, monkeypatch):
    # Only 'settings' present — get()/has() must not KeyError.
    m = _mgr(tmp_path, monkeypatch, raw=json.dumps({"settings": {"server_url": "x"}}))
    assert m.has("Garen") is False
    assert m.all() == {}
    assert m.settings == {"server_url": "x"}


def test_null_overrides_value_coerced(tmp_path, monkeypatch):
    m = _mgr(tmp_path, monkeypatch, raw=json.dumps({"overrides": None, "settings": None}))
    assert m.all() == {}
    assert m.settings == {}


def test_corrupt_json_defaults(tmp_path, monkeypatch):
    m = _mgr(tmp_path, monkeypatch, raw="{not valid json")
    assert m.all() == {}
    assert m.has("Garen") is False


def test_set_and_get_roundtrip(tmp_path, monkeypatch):
    m = _mgr(tmp_path, monkeypatch, raw=json.dumps({"settings": {}}))  # no overrides key
    m.set("Garen", {"primary_tree": "Precision"})
    assert m.get("garen") == {"primary_tree": "Precision"}
    assert "Garen" in m.all()


def test_save_is_atomic_no_tmp_left(tmp_path, monkeypatch):
    m = _mgr(tmp_path, monkeypatch)
    m.set("Garen", {"primary_tree": "Precision"})
    # The temp file must be replaced onto the target, not left behind.
    assert not (tmp_path / "overrides.json.tmp").exists()
    data = json.loads((tmp_path / "overrides.json").read_text(encoding="utf-8"))
    assert data["overrides"]["garen"] == {"primary_tree": "Precision"}
