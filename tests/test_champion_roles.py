"""
test_champion_roles.py — lane inference invariants.

Covers the regressions fixed alongside the role-weight scale work:
- inference is independent of champ-select pick order (deterministic tie-break)
- a low-weight optimal placement is not surfaced as a confident-looking guess
- reload_weights() picks up a freshly-refreshed cache mid-session
"""
import champion_roles as cr


class TestOrderIndependence:
    def setup_method(self):
        self._saved = cr._WEIGHTS

    def teardown_method(self):
        cr._WEIGHTS = self._saved

    def test_flex_tiebreak_is_order_independent(self):
        # Two identical 50/50 flex champs: the assignment must not depend on
        # which one locked in first.
        cr._WEIGHTS = {"AAA": {"mid": 50.0, "bot": 50.0},
                       "BBB": {"mid": 50.0, "bot": 50.0}}
        a1, _ = cr.infer_full_assignment(["AAA", "BBB"])
        a2, _ = cr.infer_full_assignment(["BBB", "AAA"])
        assert a1 == a2

    def test_infer_roles_order_independent(self):
        cr._WEIGHTS = {"AAA": {"mid": 50.0, "bot": 50.0},
                       "BBB": {"mid": 50.0, "bot": 50.0}}
        assert cr.infer_roles(["AAA", "BBB"], "mid") == cr.infer_roles(["BBB", "AAA"], "mid")


class TestGuessThreshold:
    def setup_method(self):
        self._saved = cr._WEIGHTS

    def teardown_method(self):
        cr._WEIGHTS = self._saved

    def test_low_weight_placement_not_guessed(self):
        # Vladimir is ~5% bot; with top/mid taken he should NOT be offered as the
        # enemy bot laner (below the 10% guess floor).
        cr._WEIGHTS = {
            "Garen": {"top": 94.0, "mid": 5.0},
            "Akali": {"mid": 70.0, "top": 29.0},
            "Vladimir": {"top": 30.0, "mid": 64.0, "bot": 5.29},
        }
        assigned, guesses = cr.infer_full_assignment(["Garen", "Akali", "Vladimir"])
        assert "bot" not in guesses
        assert "Vladimir" not in guesses.values()

    def test_relevant_flex_still_guessed(self):
        # A genuine >=10% flex placement is still surfaced as a guess.
        cr._WEIGHTS = {
            "Garen": {"top": 94.0},
            "Akali": {"mid": 70.0, "top": 29.0},
            "Swain": {"mid": 40.0, "bot": 28.0, "support": 20.0},
        }
        assigned, guesses = cr.infer_full_assignment(["Garen", "Akali", "Swain"])
        placed = set(assigned.values()) | set(guesses.values())
        assert "Swain" in placed


class TestReloadWeights:
    def test_reload_picks_up_new_cache(self, monkeypatch):
        saved = cr._WEIGHTS
        try:
            monkeypatch.setattr(
                "role_updater.get_cached_weights",
                lambda: {"Testchamp": {"mid": 95.0, "top": 5.0}},
            )
            cr.reload_weights()
            assert cr.get_role_weights("Testchamp") == {"mid": 95.0, "top": 5.0}
            assert cr.get_primary_role("Testchamp") == "mid"
        finally:
            cr._WEIGHTS = saved
