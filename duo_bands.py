"""Canonical winrate bands and synergy tiers for RuneSync botlane duo data.

Single tracked source of truth for the read-time reliability clamps applied to
the ``duos`` section of the data bundle before it reaches the champ-select UI.
Kept dependency-free so both the shipped client (``ugg_api``) and the local,
gitignored bundle builder (``scripts/build_data_bundle.py``) import the same
constants -- the build tool is not shipped in the public client, so without a
shared tracked module the build-time and read-time bands would silently drift
and a stale or broken bundle could surface a bogus duo winrate.

Rationale:
  * Duo winrates cluster tightly around 50%. A genuinely strong botlane pairing
    in an Emerald+ aggregate sits around 52-56%; anything at or above ~60% is
    almost always a small-sample fluke, so the top end is clamped hard.
  * The bottom of the sane band is 44%: below that the pair is either a
    non-synergy troll combo or noise, and is dropped as "no data" rather than
    shown to the player as a real result.
  * A pairing must beat the coin-flip to be worth recommending, so the builder
    uses a slightly stricter floor (build-time only) to keep near-50/50 noise
    out of the curated top-N list.
"""

# Duo pair winrate (this champ + partner champ, same botlane).
DUO_WR_READ_MIN = 44.0    # read-time floor (client backstop; strict >=)
DUO_WR_BUILD_MIN = 50.5   # build-time floor (stricter margin vs 50/50 noise)
DUO_WR_MAX = 60.0         # above this is a small-sample fluke either way

# Minimum games behind a duo pairing for it to be trustworthy. Duos are sampled
# far less than solo-champ stats, so this floor is lower than the matchup floor.
DUO_GAMES_BUILD_MIN = 100

# Synergy tiers, expressed as winrate thresholds above the 50% baseline. Used
# only for the human-readable label; ranking is always by raw winrate.
_TIER_THRESHOLDS = (
    (55.0, "S", "Elite pairing"),
    (53.0, "A", "Strong pairing"),
    (51.0, "B", "Solid pairing"),
    (DUO_WR_READ_MIN, "C", "Playable pairing"),
)


def is_duo_wr(wr) -> bool:
    """True if ``wr`` is a valid read-time duo winrate (in the sane band)."""
    return (isinstance(wr, (int, float)) and not isinstance(wr, bool)
            and DUO_WR_READ_MIN <= wr <= DUO_WR_MAX)


def duo_tier(wr) -> tuple[str, str]:
    """Return ``(tier_letter, label)`` for a duo winrate.

    Falls back to the lowest tier for anything at or below the read floor; the
    caller is expected to have already filtered with :func:`is_duo_wr`.
    """
    if isinstance(wr, (int, float)) and not isinstance(wr, bool):
        for threshold, letter, label in _TIER_THRESHOLDS:
            if wr >= threshold:
                return letter, label
    return "C", "Playable pairing"
