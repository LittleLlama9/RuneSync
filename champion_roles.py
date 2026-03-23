"""
champion_roles.py — pick-rate weighted role database for enemy laner inference.

ROLE_WEIGHTS maps each champion to their pick-rate weight per role, sourced from
LoLalytics Emerald+ data (patch 16.5). The "lane %" column tells us what fraction
of a champion's games are played in each role — this is the authoritative signal.

Key insight: a champion with 95% top / 5% jungle is almost certainly top lane.
A champion with 55% top / 45% jungle is a genuine flex pick — context matters.

The infer_roles() function uses a weighted constraint satisfaction algorithm:
  1. Build a probability distribution over roles for each enemy champ.
  2. Champions with very high role concentration (>80%) get strong priority.
  3. Remaining ambiguous picks are resolved by highest remaining weight.
"""

# Format: { "Champion": {"role": lane_pct, ...} }
# lane_pct = % of games played in that role (from LoLalytics lane% column)
# Only roles with meaningful play rates (>=0.2%) are included.
ROLE_WEIGHTS: dict[str, dict[str, float]] = {
    # ── Top ──────────────────────────────────────────────────────────────────
    "Aatrox":              {"top": 84.65, "jungle": 13.0},
    "Ambessa":             {"top": 53.6, "jungle": 42.73, "mid": 3.48},
    "Camille":             {"top": 85.89, "support": 6.91, "jungle": 5.1, "mid": 2.0},
    "Cho'Gath":            {"top": 68.04, "mid": 17.37, "jungle": 10.82, "support": 2.78},
    "Darius":              {"top": 87.05, "jungle": 11.68},
    "Dr. Mundo":           {"top": 49.68, "jungle": 48.32},
    "Fiora":               {"top": 98.11},
    "Gangplank":           {"top": 92.65, "mid": 6.99},
    "Garen":               {"top": 94.39, "mid": 4.7},
    "Gnar":                {"top": 98.91},
    "Gragas":              {"top": 64.06, "jungle": 19.19, "mid": 11.02, "support": 5.65},
    "Gwen":                {"top": 63.16, "jungle": 33.01, "mid": 3.61},
    "Heimerdinger":        {"top": 64.6, "mid": 15.58, "support": 12.4, "bot": 7.06},
    "Illaoi":              {"top": 97.21, "mid": 2.28},
    "Irelia":              {"top": 63.05, "mid": 36.09},
    "Jax":                 {"top": 70.6, "jungle": 27.76},
    "Jayce":               {"top": 60.3, "jungle": 28.88, "mid": 10.29},
    "K'Sante":             {"top": 95.37, "mid": 2.33, "support": 2.13},
    "Kayle":               {"top": 81.64, "mid": 16.44},
    "Kennen":              {"top": 81.46, "mid": 14.75, "support": 3.37},
    "Kled":                {"top": 92.42, "mid": 5.7},
    "Malphite":            {"top": 66.98, "jungle": 20.52, "mid": 9.17, "support": 3.26},
    "Mordekaiser":         {"top": 90.78, "jungle": 5.51, "mid": 2.99},
    "Nasus":               {"top": 71.15, "jungle": 17.37, "mid": 10.48},
    "Olaf":                {"top": 89.3, "jungle": 8.41},
    "Ornn":                {"top": 96.25},
    "Quinn":               {"top": 71.21, "mid": 22.72, "bot": 2.52, "support": 2.46},
    "Renekton":            {"top": 95.05, "mid": 4.74},
    "Riven":               {"top": 90.66, "mid": 5.3, "jungle": 3.42},
    "Rumble":              {"top": 85.29, "jungle": 7.28, "mid": 5.23, "support": 2.11},
    "Sett":                {"top": 93.52, "mid": 2.96, "support": 2.52},
    "Shen":                {"top": 49.17, "jungle": 42.92, "support": 6.6},
    "Singed":              {"top": 87.27, "mid": 7.38, "support": 2.46},
    "Sion":                {"top": 76.38, "mid": 14.92, "support": 4.13, "jungle": 3.52},
    "Teemo":               {"top": 64.4, "jungle": 21.48, "support": 11.11, "mid": 2.31},
    "Trundle":             {"top": 55.2, "jungle": 39.02, "support": 5.01},
    "Tryndamere":          {"top": 83.62, "mid": 9.92, "jungle": 5.6},
    "Urgot":               {"top": 96.41},
    "Volibear":            {"top": 56.72, "jungle": 42.16},
    "Yorick":              {"top": 93.74, "mid": 3.69, "jungle": 2.12},
    # ── Jungle ───────────────────────────────────────────────────────────────
    "Amumu":               {"jungle": 84.2, "support": 14.95},
    "Bel'Veth":            {"jungle": 96.52},
    "Briar":               {"jungle": 89.34, "top": 5.39, "mid": 3.96},
    "Diana":               {"jungle": 51.85, "mid": 45.96},
    "Ekko":                {"jungle": 57.25, "mid": 40.46},
    "Elise":               {"jungle": 69.86, "support": 26.29, "top": 2.87},
    "Evelynn":             {"jungle": 97.95},
    "Fiddlesticks":        {"jungle": 67.31, "support": 15.98, "top": 13.57, "mid": 3.02},
    "Graves":              {"jungle": 96.71, "top": 2.31},
    "Hecarim":             {"jungle": 99.49},
    "Ivern":               {"jungle": 78.5, "support": 12.06, "top": 6.55, "mid": 2.61},
    "Jarvan IV":           {"jungle": 93.31, "support": 4.59},
    "Karthus":             {"jungle": 70.7, "bot": 17.45, "mid": 7.38, "top": 3.21},
    "Kayn":                {"jungle": 93.46, "top": 5.99},
    "Kha'Zix":             {"jungle": 99.52},
    "Kindred":             {"jungle": 96.51},
    "Lee Sin":             {"jungle": 95.53, "top": 2.42},
    "Lillia":              {"jungle": 97.91},
    "Master Yi":           {"jungle": 84.87, "top": 11.85},
    "Naafiri":             {"jungle": 59.13, "mid": 34.38, "top": 5.48},
    "Nidalee":             {"jungle": 84.8, "support": 10.68, "top": 2.93},
    "Nocturne":            {"jungle": 97.25},
    "Nunu & Willump":      {"jungle": 88.15, "mid": 7.98, "support": 3.41},
    "Rammus":              {"jungle": 89.68, "support": 5.22, "top": 4.42},
    "Rek'Sai":             {"jungle": 87.94, "top": 9.77},
    "Rengar":              {"jungle": 89.76, "top": 7.19, "support": 2.2},
    "Sejuani":             {"jungle": 79.84, "top": 14.79, "mid": 3.08, "support": 2.24},
    "Shaco":               {"jungle": 76.01, "support": 21.54},
    "Shyvana":             {"jungle": 90.88, "top": 8.25},
    "Skarner":             {"jungle": 72.06, "top": 15.23, "support": 11.67},
    "Talon":               {"jungle": 66.77, "mid": 32.09},
    "Udyr":                {"jungle": 68.8, "top": 29.06},
    "Vi":                  {"jungle": 95.59, "top": 2.27},
    "Viego":               {"jungle": 96.28, "mid": 2.09},
    "Warwick":             {"jungle": 66.36, "top": 32.41},
    "Wukong":              {"jungle": 72.99, "top": 22.54, "mid": 2.04, "support": 2.03},
    "Xin Zhao":            {"jungle": 92.82, "top": 5.03},
    "Zaahen":              {"jungle": 50.01, "top": 48.74},
    "Zac":                 {"jungle": 72.57, "top": 14.38, "support": 8.09, "mid": 4.86},
    # ── Mid ──────────────────────────────────────────────────────────────────
    "Ahri":                {"mid": 98.11},
    "Akali":               {"mid": 70.8, "top": 28.96},
    "Akshan":              {"mid": 90.51, "top": 6.43, "bot": 2.11},
    "Anivia":              {"mid": 72.34, "top": 14.21, "support": 11.46},
    "Annie":               {"mid": 81.48, "support": 13.38, "top": 4.09},
    "Aurelion Sol":        {"mid": 75.57, "bot": 17.66, "top": 3.86},
    "Aurora":              {"mid": 79.42, "top": 19.16},
    "Azir":                {"mid": 93.19, "top": 5.65},
    "Cassiopeia":          {"mid": 57.58, "top": 27.76, "bot": 13.66},
    "Fizz":                {"mid": 69.65, "jungle": 27.66},
    "Galio":               {"mid": 83.85, "support": 11.04, "top": 4.87},
    "Hwei":                {"mid": 78.38, "support": 10.1, "bot": 10.06},
    "Kassadin":            {"mid": 95.18, "top": 4.26},
    "Katarina":            {"mid": 92.91, "bot": 5.21},
    "LeBlanc":             {"mid": 85.95, "support": 12.29},
    "Lissandra":           {"mid": 90.08, "support": 5.06, "top": 4.42},
    "Malzahar":            {"mid": 92.21, "top": 5.36},
    "Mel":                 {"mid": 60.64, "support": 22.83, "bot": 15.84},
    "Orianna":             {"mid": 97.04},
    "Qiyana":              {"mid": 56.05, "jungle": 38.33, "top": 3.48},
    "Ryze":                {"mid": 80.45, "top": 18.76},
    "Sylas":               {"mid": 52.23, "jungle": 32.71, "top": 8.05, "support": 6.87},
    "Syndra":              {"mid": 93.71, "bot": 3.41},
    "Taliyah":             {"mid": 63.25, "jungle": 29.92, "support": 3.86},
    "Twisted Fate":        {"mid": 93.34, "top": 3.31, "support": 2.14},
    "Veigar":              {"mid": 67.65, "bot": 24.85, "support": 5.14, "top": 2.34},
    "Vex":                 {"mid": 96.39},
    "Viktor":              {"mid": 95.77, "bot": 2.05, "top": 2.02},
    "Vladimir":            {"mid": 64.33, "top": 30.28, "bot": 5.29},
    "Xerath":              {"mid": 62.46, "support": 33.91, "bot": 3.34},
    "Yasuo":               {"mid": 67.14, "top": 22.87, "bot": 9.6},
    "Yone":                {"mid": 50.15, "top": 48.65},
    "Zed":                 {"mid": 71.3, "jungle": 23.22, "top": 4.66},
    "Zoe":                 {"mid": 82.29, "support": 15.42},
    # ── Bot ──────────────────────────────────────────────────────────────────
    "Aphelios":            {"bot": 99.01},
    "Ashe":                {"bot": 91.83, "support": 6.76},
    "Caitlyn":             {"bot": 98.74},
    "Corki":               {"bot": 89.0, "mid": 8.49, "top": 2.23},
    "Draven":              {"bot": 96.11, "top": 2.0},
    "Ezreal":              {"bot": 93.04, "support": 4.91},
    "Jhin":                {"bot": 98.35},
    "Jinx":                {"bot": 99.71},
    "Kai'Sa":              {"bot": 97.6},
    "Kalista":             {"bot": 87.85, "top": 7.61, "mid": 3.62},
    "Kog'Maw":             {"bot": 72.41, "mid": 22.15, "top": 3.91},
    "Lucian":              {"bot": 97.05},
    "Miss Fortune":        {"bot": 97.2},
    "Nilah":               {"bot": 98.83},
    "Samira":              {"bot": 98.99},
    "Sivir":               {"bot": 99.3},
    "Smolder":             {"bot": 85.18, "mid": 9.44, "top": 5.23},
    "Tristana":            {"bot": 87.66, "mid": 10.32},
    "Twitch":              {"bot": 87.91, "jungle": 4.99, "support": 4.44, "mid": 2.39},
    "Varus":               {"bot": 66.8, "top": 27.39, "mid": 5.08},
    "Vayne":               {"bot": 65.92, "top": 31.17},
    "Xayah":               {"bot": 99.54},
    "Yunara":              {"bot": 99.49},
    "Zeri":                {"bot": 96.45},
    "Ziggs":               {"bot": 63.94, "mid": 31.22, "support": 4.11},
    # ── Support ──────────────────────────────────────────────────────────────
    "Alistar":             {"support": 98.05},
    "Bard":                {"support": 99.48},
    "Blitzcrank":          {"support": 99.47},
    "Brand":               {"support": 50.72, "mid": 18.22, "bot": 16.34, "jungle": 10.98, "top": 3.75},
    "Braum":               {"support": 99.86},
    "Janna":               {"support": 99.22},
    "Karma":               {"support": 95.77, "mid": 2.27},
    "Leona":               {"support": 99.74},
    "Lulu":                {"support": 98.9},
    "Lux":                 {"support": 52.07, "mid": 40.89, "bot": 6.66},
    "Maokai":              {"support": 64.89, "top": 18.04, "jungle": 16.4},
    "Milio":               {"support": 99.86},
    "Morgana":             {"support": 83.65, "mid": 9.48, "jungle": 5.21},
    "Nami":                {"support": 99.91},
    "Nautilus":            {"support": 97.93},
    "Neeko":               {"support": 78.14, "mid": 14.89, "jungle": 3.61, "top": 2.86},
    "Pantheon":            {"support": 42.79, "top": 28.6, "jungle": 16.04, "mid": 12.29},
    "Poppy":               {"support": 46.38, "top": 39.87, "jungle": 12.83},
    "Pyke":                {"support": 98.44},
    "Rakan":               {"support": 98.73},
    "Rell":                {"support": 99.16},
    "Renata Glasc":        {"support": 99.0},
    "Senna":               {"support": 75.57, "bot": 22.68},
    "Seraphine":           {"support": 87.83, "bot": 8.64, "mid": 3.39},
    "Sona":                {"support": 99.48},
    "Soraka":              {"support": 98.25},
    "Swain":               {"support": 43.85, "bot": 28.81, "mid": 16.63, "top": 10.68},
    "Tahm Kench":          {"support": 48.21, "top": 47.79, "bot": 2.21},
    "Taric":               {"support": 95.8, "jungle": 2.55},
    "Thresh":              {"support": 99.73},
    "Vel'Koz":             {"support": 59.61, "mid": 25.24, "bot": 12.82, "top": 2.31},
    "Yuumi":               {"support": 99.58},
    "Zilean":              {"support": 84.11, "mid": 10.65, "top": 4.33},
    "Zyra":                {"support": 62.73, "jungle": 32.37, "mid": 2.82},
}


def _load_weights() -> dict[str, dict[str, float]]:
    """
    Return role weights from live cache if available, else fall back to
    the hardcoded ROLE_WEIGHTS above.
    """
    try:
        from role_updater import get_cached_weights
        cached = get_cached_weights()
        if cached:
            return cached
    except Exception:
        pass
    return ROLE_WEIGHTS  # fallback to hardcoded


def needs_role_refresh() -> bool:
    """Returns True if role weight cache is stale (patch changed or missing)."""
    try:
        from role_updater import cache_is_stale
        return cache_is_stale()
    except Exception:
        return False


# Merged weights: live cache preferred, hardcoded as fallback
_WEIGHTS = _load_weights()


def get_role_weights(champion_name: str) -> dict[str, float]:
    """Return the role weight dict for a champion, or {} if unknown."""
    return _WEIGHTS.get(champion_name) or _WEIGHTS.get(champion_name.title(), {})


def get_primary_role(champion_name: str) -> str:
    """Return the most-played role for a champion, or 'unknown'."""
    weights = get_role_weights(champion_name)
    if not weights:
        return "unknown"
    return max(weights, key=weights.__getitem__)


def infer_roles(enemy_picks: list[str], my_role: str) -> str | None:
    """
    Given a list of enemy champion names and your role, use weighted constraint
    satisfaction to determine which enemy is most likely in your lane.

    Algorithm:
      1. Champions with >85% weight in a single role strongly claim it.
      2. Remaining champs are assigned iteratively to their highest-weighted
         unclaimed role.
      3. Return whichever enemy ended up assigned to my_role.

    Example: Tryndamere (top 95%, jng 5%) + Aatrox (top 85%, jng 15%)
      → Tryndamere claims top (95% >> threshold), Aatrox gets jungle (next best).
    """
    if not enemy_picks or my_role in ("auto", "unknown"):
        return None

    CLAIM_THRESHOLD = 85.0  # above this %, a champ "claims" their role

    # Build weight maps, skip unknowns
    champ_weights: dict[str, dict[str, float]] = {}
    for c in enemy_picks:
        w = get_role_weights(c)
        if w:
            champ_weights[c] = w

    if not champ_weights:
        return None

    assigned: dict[str, str] = {}   # role -> champion name

    # Pass 1: high-confidence single-role claims
    # Collect ALL claimants per role, then pick the highest-confidence one.
    # This prevents order bias: Garen (94% top) beats Darius (88% top)
    # regardless of which was picked first in champ select.
    claimants: dict[str, list[tuple[float, str]]] = {}
    for champ, weights in champ_weights.items():
        best_role = max(weights, key=weights.__getitem__)
        if weights[best_role] >= CLAIM_THRESHOLD:
            claimants.setdefault(best_role, []).append((weights[best_role], champ))

    for role, candidates in claimants.items():
        _, best_champ = max(candidates, key=lambda x: x[0])
        assigned[role] = best_champ

    # Pass 2: remaining champs fill best available unclaimed role
    unassigned = [c for c in champ_weights if c not in assigned.values()]
    # Sort by their best-role confidence descending so most certain picks go first
    unassigned.sort(
        key=lambda c: max(champ_weights[c].values()), reverse=True
    )
    for champ in unassigned:
        for role, _ in sorted(champ_weights[champ].items(),
                               key=lambda x: x[1], reverse=True):
            if role not in assigned:
                assigned[role] = champ
                break

    return assigned.get(my_role)


def infer_full_assignment(enemy_picks: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """
    Run the same constraint satisfaction as infer_roles() but return the full
    role -> champion assignment dict instead of just one lane.
    Useful for monitor.py to check if a role has been filled yet.

    Returns (confident, guesses):
      - confident: high-confidence role assignments (passes 1 & 2)
      - guesses:   fallback assignments for roles still unfilled after pass 2,
                   where any enemy champion has >= 10% play rate in that role.
                   These should be shown with a warning in the UI.
    """
    if not enemy_picks:
        return {}, {}

    CLAIM_THRESHOLD = 85.0
    GUESS_THRESHOLD = 10.0

    champ_weights: dict[str, dict[str, float]] = {}
    for c in enemy_picks:
        w = get_role_weights(c)
        if w:
            champ_weights[c] = w

    if not champ_weights:
        return {}, {}

    assigned: dict[str, str] = {}

    claimants: dict[str, list[tuple[float, str]]] = {}
    for champ, weights in champ_weights.items():
        best_role = max(weights, key=weights.__getitem__)
        if weights[best_role] >= CLAIM_THRESHOLD:
            claimants.setdefault(best_role, []).append((weights[best_role], champ))

    for role, candidates in claimants.items():
        _, best_champ = max(candidates, key=lambda x: x[0])
        assigned[role] = best_champ

    unassigned = [c for c in champ_weights if c not in assigned.values()]
    unassigned.sort(key=lambda c: max(champ_weights[c].values()), reverse=True)
    for champ in unassigned:
        for role, _ in sorted(champ_weights[champ].items(), key=lambda x: x[1], reverse=True):
            if role not in assigned:
                assigned[role] = champ
                break

    # Pass 3: for roles still unfilled, find any enemy champ with >= 10% in
    # that role (including champs already assigned elsewhere — flex picks).
    guesses: dict[str, str] = {}
    for role in ("top", "jungle", "mid", "bot", "support"):
        if role in assigned:
            continue
        candidates = [
            (weights.get(role, 0.0), champ)
            for champ, weights in champ_weights.items()
            if weights.get(role, 0.0) >= GUESS_THRESHOLD
        ]
        if candidates:
            guesses[role] = max(candidates)[1]

    return assigned, guesses
