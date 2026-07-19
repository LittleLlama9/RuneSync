"""Tests for ugg_api.get_skill_order — bundle-backed skill-level lookup for the
in-game HUD's "which ability to level next" line."""
import ugg_api


def _install_bundle(monkeypatch, bundle):
    monkeypatch.setattr(ugg_api, "_bundle", bundle, raising=False)
    ugg_api._bundle_ready_event.set()


def _bundle(skill_order=None, skill_max=None, role="mid"):
    build = {"champion": "Ahri", "role": role}
    if skill_order is not None:
        build["skill_order"] = skill_order
    if skill_max is not None:
        build["skill_max"] = skill_max
    return {"builds": {"ahri": {role: build}}}


def test_returns_order_and_max(monkeypatch):
    _install_bundle(monkeypatch, _bundle(
        skill_order=["Q", "W", "E", "Q", "Q", "R"], skill_max=["Q", "W", "E"]))
    got = ugg_api.UGGClient().get_skill_order("Ahri", "mid")
    assert got == {"order": ["Q", "W", "E", "Q", "Q", "R"], "max": ["Q", "W", "E"]}


def test_role_fallback_finds_skill_data(monkeypatch):
    # Requested role has no skill data; another role does -> fall back to it.
    bundle = {"builds": {"ahri": {
        "top": {"champion": "Ahri", "role": "top", "skill_order": [], "skill_max": []},
        "mid": {"champion": "Ahri", "role": "mid",
                "skill_order": ["Q", "W"], "skill_max": ["Q"]},
    }}}
    _install_bundle(monkeypatch, bundle)
    got = ugg_api.UGGClient().get_skill_order("Ahri", "top")
    assert got == {"order": ["Q", "W"], "max": ["Q"]}


def test_auto_role_uses_any_available(monkeypatch):
    _install_bundle(monkeypatch, _bundle(
        skill_order=["E", "Q", "W"], skill_max=["E"], role="jungle"))
    got = ugg_api.UGGClient().get_skill_order("Ahri", "auto")
    assert got == {"order": ["E", "Q", "W"], "max": ["E"]}


def test_missing_skill_data_returns_none(monkeypatch):
    # Older bundle built before skill scraping -> empty lists -> None.
    _install_bundle(monkeypatch, _bundle(skill_order=[], skill_max=[]))
    assert ugg_api.UGGClient().get_skill_order("Ahri", "mid") is None


def test_missing_champ_returns_none(monkeypatch):
    _install_bundle(monkeypatch, _bundle(skill_order=["Q"], skill_max=["Q"]))
    assert ugg_api.UGGClient().get_skill_order("Zed", "mid") is None


def test_no_bundle_returns_none(monkeypatch):
    monkeypatch.setattr(ugg_api, "_bundle", None, raising=False)
    ugg_api._bundle_ready_event.set()
    assert ugg_api.UGGClient().get_skill_order("Ahri", "mid") is None
