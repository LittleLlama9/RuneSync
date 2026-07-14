"""Canonical winrate bands for RuneSync matchup and counter data.

Single tracked source of truth for the read-time reliability clamps applied to
bundle data before it reaches the champ-select UI. Kept dependency-free so both
the shipped client (``ugg_api``) and the local, gitignored bundle builder
(``scripts/build_data_bundle.py``) can import the same constants — the build
tool is not shipped in the public client, so without a shared tracked module the
build-time and read-time bands would silently drift and a stale or broken bundle
could surface a bogus winrate.

Rationale:
  * A counter must WIN the lane, so a read-time counter floor is a hard 50 —
    anything at or below is not a counter and is dropped even before the client
    re-downloads a corrected bundle. The builder uses a slightly stricter 52
    floor so noise near 50/50 doesn't seed the curated list; the extra margin is
    build-time only.
  * Above ~70% a "counter" is almost always a small-sample fluke (the old op.gg
    path shipped counter winrates up to ~82% off ~10-game samples), so both ends
    are clamped.
  * Per-lane matchup winrates keep a wider [25, 75] band so legitimately lopsided
    lanes still show, while out-of-band garbage is rejected as "no data" rather
    than shown to the player as a real result.
"""

# Counter list ("who beats this champ", top-5 curated / matchup-derived).
COUNTER_WR_READ_MIN = 50.0    # read-time floor (client backstop; strict >)
COUNTER_WR_BUILD_MIN = 52.0   # build-time floor (stricter margin vs 50/50 noise)
COUNTER_WR_MAX = 70.0         # above this is a small-sample fluke either way

# Per-lane matchup winrate (my champ vs this exact enemy laner).
MATCHUP_WR_MIN = 25.0
MATCHUP_WR_MAX = 75.0


def is_counter_wr(wr) -> bool:
    """True if ``wr`` is a valid read-time counter winrate.

    A counter must strictly win the lane (> the read floor) and fall within the
    sane band. Non-numeric input is rejected.
    """
    return (isinstance(wr, (int, float))
            and COUNTER_WR_READ_MIN < wr <= COUNTER_WR_MAX)


def is_matchup_wr(wr) -> bool:
    """True if ``wr`` is a plausible per-lane matchup winrate (in the sane band)."""
    return (isinstance(wr, (int, float))
            and MATCHUP_WR_MIN <= wr <= MATCHUP_WR_MAX)
