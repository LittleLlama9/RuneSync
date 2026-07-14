"""Tests for the canonical winrate band predicates in matchup_bands.

These lock the read-time reliability clamp that both the shipped client and the
local bundle builder rely on. The client (ugg_api) delegates its counter and
matchup validation to is_counter_wr / is_matchup_wr, so these boundaries are the
single source of truth for "what winrate is trustworthy enough to show."
"""

import matchup_bands as mb
from matchup_bands import is_counter_wr, is_matchup_wr


class TestBandConstants:
    def test_counter_read_floor_is_50(self):
        assert mb.COUNTER_WR_READ_MIN == 50.0

    def test_counter_build_floor_is_stricter_than_read(self):
        assert mb.COUNTER_WR_BUILD_MIN > mb.COUNTER_WR_READ_MIN

    def test_counter_max_is_70(self):
        assert mb.COUNTER_WR_MAX == 70.0

    def test_matchup_band_is_25_to_75(self):
        assert (mb.MATCHUP_WR_MIN, mb.MATCHUP_WR_MAX) == (25.0, 75.0)


class TestIsCounterWr:
    def test_rejects_at_and_below_floor(self):
        # A counter must strictly WIN the lane; 50.0 is a coin flip, not a counter.
        assert not is_counter_wr(50.0)
        assert not is_counter_wr(49.9)
        assert not is_counter_wr(44.81)   # the Malphite "popular loser" bug value

    def test_accepts_just_above_floor(self):
        assert is_counter_wr(50.01)
        assert is_counter_wr(56.41)

    def test_accepts_at_max(self):
        assert is_counter_wr(70.0)

    def test_rejects_above_max(self):
        # ~82% off a ~10-game sample — the old op.gg garbage the band exists to kill.
        assert not is_counter_wr(70.01)
        assert not is_counter_wr(82.0)

    def test_rejects_non_numeric(self):
        assert not is_counter_wr(None)
        assert not is_counter_wr("55")
        assert not is_counter_wr([])

    def test_bool_true_is_out_of_band(self):
        # bool is a subclass of int; True == 1 is well below the floor, so it is
        # correctly rejected rather than treated as a valid winrate.
        assert not is_counter_wr(True)


class TestIsMatchupWr:
    def test_accepts_in_band(self):
        assert is_matchup_wr(25.0)
        assert is_matchup_wr(50.0)
        assert is_matchup_wr(75.0)

    def test_rejects_out_of_band(self):
        assert not is_matchup_wr(24.9)
        assert not is_matchup_wr(75.1)
        assert not is_matchup_wr(82.0)   # surfaces as "no data" rather than a fake label

    def test_rejects_non_numeric(self):
        assert not is_matchup_wr(None)
        assert not is_matchup_wr("50")
