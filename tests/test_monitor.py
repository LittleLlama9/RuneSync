"""
test_monitor.py — Concurrency tests for ChampSelectMonitor._import_runes.

These cover the serialization fix: the Reimport button pushes _import_runes on
its own thread while the poll loop also calls it on lane-swap / champ-detect.
Without mutual exclusion, two threads drive the same LCUClient at once and can
interleave a half-applied item set or duplicate rune page.

No Tk and no network — the LCU/uGG/overrides collaborators are mocked, and
_apply_ugg is stubbed so the test controls timing.
"""
import threading
import time
from unittest.mock import MagicMock

from monitor import ChampSelectMonitor


def _make_monitor():
    overrides = MagicMock()
    overrides.get.return_value = None  # force the _apply_ugg (non-override) path
    return ChampSelectMonitor(
        lcu=MagicMock(), ugg=MagicMock(), overrides=overrides,
        on_log=lambda *a, **k: None,
    )


def test_import_runes_serializes_across_threads():
    """Concurrent _import_runes calls must never overlap — exactly one in flight."""
    mon = _make_monitor()

    counter = {"active": 0, "max": 0}
    guard = threading.Lock()

    def fake_apply(champ, session):
        with guard:
            counter["active"] += 1
            counter["max"] = max(counter["max"], counter["active"])
        time.sleep(0.03)  # hold the critical section long enough to collide
        with guard:
            counter["active"] -= 1

    mon._apply_ugg = fake_apply

    threads = [
        threading.Thread(target=mon._import_runes, args=(f"Champ{i}", {}))
        for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Without the lock this would be 5 (all threads inside fake_apply at once).
    assert counter["max"] == 1


def test_import_runes_is_reentrant_same_thread():
    """A same-thread re-entry must not self-deadlock — this is why it's an RLock,
    not a plain Lock. Run in a daemon thread with a join timeout so a regression
    fails the test cleanly instead of hanging the whole suite."""
    mon = _make_monitor()
    calls = []

    def fake_apply(champ, session):
        calls.append(champ)
        if len(calls) == 1:
            # Re-enter on the SAME thread while the lock is held.
            mon._import_runes("Inner", {})

    mon._apply_ugg = fake_apply

    done = threading.Event()

    def run():
        mon._import_runes("Outer", {})
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=2.0)

    assert done.is_set(), "re-entrant _import_runes deadlocked — needs RLock, not Lock"
    assert calls == ["Outer", "Inner"]


# ── off-role fallback summoner fix ──────────────────────────────────────────

def test_offrole_summoners_swap_smite_for_lane_spell():
    """A jungle fallback build imported for a laner must drop Smite (11) for the
    lane's standard second summoner, keeping Flash."""
    mon = _make_monitor()
    assert mon._fix_offrole_summoners([4, 11], "top") == [4, 12]      # Flash + TP
    assert mon._fix_offrole_summoners([4, 11], "mid") == [4, 14]      # Flash + Ignite
    assert mon._fix_offrole_summoners([4, 11], "bot") == [4, 7]       # Flash + Heal
    assert mon._fix_offrole_summoners([4, 11], "support") == [4, 3]   # Flash + Exhaust


def test_offrole_summoners_untouched_when_no_smite_or_jungle():
    mon = _make_monitor()
    # No Smite present — leave as-is.
    assert mon._fix_offrole_summoners([4, 12], "top") == [4, 12]
    # Jungle role legitimately keeps Smite.
    assert mon._fix_offrole_summoners([4, 11], "jungle") == [4, 11]
    # Unknown/auto role — don't guess.
    assert mon._fix_offrole_summoners([4, 11], "auto") == [4, 11]


def test_assigned_enemy_role_beats_ambiguous_role_weights(monkeypatch):
    """Two ADCs must not hide the matchup when Riot exposes the real mid."""
    callback = MagicMock()
    mon = ChampSelectMonitor(
        lcu=MagicMock(), ugg=MagicMock(), overrides=MagicMock(),
        on_log=lambda *a, **k: None, on_matchup_winrate=callback,
    )
    mon._my_role = "mid"
    mon._my_champ = "Sion"
    mon._champ_name_map = {
        902: "Milio", 145: "Kai'Sa", 154: "Zac", 360: "Samira", 86: "Garen",
    }
    mon.lcu.get_enemy_champion_id_for_role.return_value = 360
    mon.ugg.get_matchup_winrate.return_value = {"win_rate": 48.5}

    class ImmediateThread:
        def __init__(self, target, args, daemon):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(threading, "Thread", ImmediateThread)
    session = {
        "myTeam": [{"cellId": 0}],
        "theirTeam": [],
        "actions": [[
            {"actorCellId": cell, "type": "pick", "championId": champion_id,
             "completed": True}
            for cell, champion_id in enumerate((902, 145, 154, 360, 86), 5)
        ]],
    }

    mon._update_enemy_laner(session)

    assert mon._enemy_laner == "Samira"
    callback.assert_called_once()
    assert callback.call_args.args[:3] == ("Sion", "Samira", "mid")


def test_champ_select_assigned_position_is_used_before_gameflow():
    mon = _make_monitor()
    mon._my_role = "mid"
    mon._champ_name_map = {360: "Samira"}
    mon.lcu.get_enemy_champion_id_for_role.return_value = None
    session = {
        "theirTeam": [
            {"cellId": 8, "championId": 360, "assignedPosition": "MIDDLE"},
        ],
    }

    assert mon._get_assigned_enemy_laner(session, frozenset({"Samira"})) == "Samira"
    mon.lcu.get_enemy_champion_id_for_role.assert_not_called()


def test_matchup_callback_still_fires_without_winrate_data():
    callback = MagicMock()
    mon = ChampSelectMonitor(
        lcu=MagicMock(), ugg=MagicMock(), overrides=MagicMock(),
        on_log=lambda *a, **k: None, on_matchup_winrate=callback,
    )
    mon.ugg.get_matchup_winrate.return_value = None

    mon._run_matchup_lookup("Sion", "Samira", "mid")

    callback.assert_called_once_with(
        "Sion", "Samira", "mid", None, "Win rate unavailable", "info",
    )
