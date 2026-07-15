"""Transparent, role-aware post-game performance scoring for RuneSync."""

from collections import defaultdict


SCORING_MODEL_VERSION = 1

COMPONENT_WEIGHTS = {
    "lane": {
        "combat": 0.35, "economy": 0.25, "objectives": 0.15,
        "vision": 0.10, "teamplay": 0.15,
    },
    "jungle": {
        "combat": 0.30, "economy": 0.12, "objectives": 0.28,
        "vision": 0.12, "teamplay": 0.18,
    },
    "support": {
        "combat": 0.25, "economy": 0.03, "objectives": 0.15,
        "vision": 0.30, "teamplay": 0.27,
    },
    "unknown": {
        "combat": 0.30, "economy": 0.20, "objectives": 0.20,
        "vision": 0.15, "teamplay": 0.15,
    },
}


def _safe_div(numerator, denominator) -> float:
    return float(numerator or 0) / max(float(denominator or 0), 1.0)


def _percentiles(values: list[float], lower_is_better: bool = False) -> list[float]:
    """Return midrank percentiles so ties receive identical deterministic values."""
    if len(values) <= 1:
        return [50.0 for _ in values]
    out = []
    for value in values:
        below = sum(1 for other in values if other < value)
        equal = sum(1 for other in values if other == value)
        rank = below + (equal - 1) / 2
        percentile = rank * 100 / (len(values) - 1)
        out.append(100 - percentile if lower_is_better else percentile)
    return out


def _role_bucket(role: str) -> str:
    if role == "jungle":
        return "jungle"
    if role == "support":
        return "support"
    if role in {"top", "mid", "bot"}:
        return "lane"
    return "unknown"


def _component_observations(player: dict, components: dict, all_players: list[dict]) -> list[str]:
    labels = {
        "combat": "Combat", "economy": "Economy", "objectives": "Objectives",
        "vision": "Vision", "teamplay": "Survival/teamplay",
    }
    strongest = max(components, key=components.get)
    weakest = min(components, key=components.get)
    spread = components[strongest] - components[weakest]
    observations = []
    if spread >= 0.1:
        observations.extend([
            f"{labels[strongest]} was the strongest component.",
            f"{labels[weakest]} ranked lowest among the score components.",
        ])
    else:
        observations.append("Score components were evenly balanced.")

    facts = (
        ("damage_to_champions", "Highest champion damage in the match."),
        ("vision_score", "Highest vision score in the match."),
        ("damage_to_objectives", "Highest objective damage in the match."),
    )
    for key, text in facts:
        value = player.get(key, 0)
        leaders = [
            p for p in all_players
            if p.get(key, 0) == max(x.get(key, 0) for x in all_players)
        ]
        if value > 0 and len(leaders) == 1 and leaders[0] is player:
            observations.append(text)

    team = [p for p in all_players if p["team_id"] == player["team_id"]]
    team_kills = sum(p["kills"] for p in team)
    participation = _safe_div(player["kills"] + player["assists"], team_kills)
    participation_values = [
        _safe_div(p["kills"] + p["assists"], team_kills) for p in team
    ]
    if team_kills > 0 and participation_values.count(participation) == 1 \
            and participation == max(participation_values):
        observations.append("Highest kill participation on the team.")
    return observations[:4]


def score_match(participants: list[dict], duration_seconds: int) -> list[dict]:
    """Score 10 normalized participants and assign deterministic ranks 1-10."""
    if len(participants) != 10:
        raise ValueError("DAEMON Score requires exactly 10 participants")
    minutes = max(float(duration_seconds) / 60.0, 1.0)

    team_totals = defaultdict(lambda: defaultdict(float))
    for player in participants:
        totals = team_totals[player["team_id"]]
        totals["kills"] += player["kills"]
        totals["damage"] += player["damage_to_champions"]
        totals["objectives"] += player["damage_to_objectives"]
        totals["turrets"] += player["damage_to_turrets"]

    metrics = defaultdict(list)
    raw_rows = []
    for player in participants:
        totals = team_totals[player["team_id"]]
        row = {
            "kill_participation": _safe_div(
                player["kills"] + player["assists"], totals["kills"],
            ),
            "kda": _safe_div(player["kills"] + player["assists"], player["deaths"]),
            "damage_share": _safe_div(
                player["damage_to_champions"], totals["damage"],
            ),
            "damage_efficiency": _safe_div(
                player["damage_to_champions"], player["gold_earned"],
            ),
            "gold_per_minute": _safe_div(player["gold_earned"], minutes),
            "cs_per_minute": _safe_div(player["cs"], minutes),
            "objective_share": _safe_div(
                player["damage_to_objectives"], totals["objectives"],
            ),
            "turret_share": _safe_div(
                player["damage_to_turrets"], totals["turrets"],
            ),
            "vision_per_minute": _safe_div(player["vision_score"], minutes),
            "ward_actions_per_minute": _safe_div(
                player["wards_placed"] + player["wards_killed"], minutes,
            ),
            "deaths_per_minute": _safe_div(player["deaths"], minutes),
            "utility_per_minute": _safe_div(
                player["damage_mitigated"] + player["healing"], minutes,
            ),
        }
        raw_rows.append(row)
        for key, value in row.items():
            metrics[key].append(value)

    normalized = {
        key: _percentiles(values, lower_is_better=(key == "deaths_per_minute"))
        for key, values in metrics.items()
    }

    scored = []
    for index, player in enumerate(participants):
        component_values = {
            "combat": (
                normalized["kill_participation"][index]
                + normalized["kda"][index]
                + normalized["damage_share"][index]
                + normalized["damage_efficiency"][index]
            ) / 4,
            "economy": (
                normalized["gold_per_minute"][index]
                + normalized["cs_per_minute"][index]
            ) / 2,
            "objectives": (
                normalized["objective_share"][index]
                + normalized["turret_share"][index]
            ) / 2,
            "vision": (
                normalized["vision_per_minute"][index]
                + normalized["ward_actions_per_minute"][index]
            ) / 2,
            "teamplay": (
                normalized["deaths_per_minute"][index]
                + normalized["utility_per_minute"][index]
                + normalized["kill_participation"][index]
            ) / 3,
        }
        weights = COMPONENT_WEIGHTS[_role_bucket(player.get("role", ""))]
        weighted = sum(component_values[name] * weights[name] for name in weights)
        total = max(0.0, min(100.0, 5.0 + weighted * 0.90 + (4.0 if player["win"] else 0.0)))
        scored.append({
            "participant_id": player["participant_id"],
            "model_version": SCORING_MODEL_VERSION,
            "total_score": round(total, 1),
            "_raw_score": total,
            "_weighted": weighted,
            "_damage": player["damage_to_champions"],
            "_kill_participation": raw_rows[index]["kill_participation"],
            "components": {
                name: round(value, 1) for name, value in component_values.items()
            },
            "observations": _component_observations(
                player, component_values, participants,
            ),
        })

    scored.sort(
        key=lambda row: (
            -row["_raw_score"], -row["_weighted"], -row["_damage"],
            -row["_kill_participation"], row["participant_id"],
        )
    )
    for rank, row in enumerate(scored, 1):
        row["match_rank"] = rank
        for private_key in ("_raw_score", "_weighted", "_damage", "_kill_participation"):
            row.pop(private_key)
    return scored
