"""Canonical DAEMON Score v2 feature contract.

This module is the single allowlisted bridge between `score_features.py`'s
rich, nested per-participant evidence blocks and the compact numeric
feature vector `score_v2.training.baseline` fits and `score_v2.runtime`
evaluates. It is intentionally narrow:

  * Every model input is a hand-reviewed dotted `path` into one
    participant's `score_features.compute_feature_set(...)` block --
    never a dynamically-discovered key. Adding a feature means adding a
    reviewed `FeatureSpec` here, not widening a glob.
  * `score_v2.leakage.validate_feature_payload` is run on every payload
    before any path is read, so a corrupted or hand-edited payload still
    cannot smuggle an outcome field in even if a future `FeatureSpec` were
    added carelessly.
  * `direction` records each feature's *expected* monotonic relationship
    with performance (+1 higher-is-better, -1 lower-is-better, 0
    unconstrained) -- `score_v2.training.baseline` projects fitted
    coefficients to match this sign, and `score_v2.artifact` re-verifies
    the stored coefficient sign against it on load.

Deliberately excluded raw stats: `raw.vision_score`, `raw.wards_placed`,
`raw.wards_killed`, `raw.damage_to_turrets`, `raw.damage_to_objectives`,
`raw.damage_to_champions`, `raw.gold_earned`, and `raw.cs` are present in
`score_features.py`'s `raw` block for provenance, but are NOT model
features here. Feeding raw vision score or turret damage share directly
into a linear model would reproduce exactly the DAEMON v1 regression this
project exists to fix (see the vault K'Sante/Seraphine/Vel'Koz case:
Seraphine's raw vision score inflated her v1 score despite zero
actionable map impact, and Vel'Koz's turret damage share was credited as
objective influence with no real secure/assist). Raw gold/CS are excluded
for the same reason: neither is causally validated as influence on its
own -- only `resource_conversion_rate` (a causally-filtered gold-LEAD
conversion signal) represents economy influence here. Likewise, a raw
monster-kill "assist" credit (`epic_monster_assists`/`grub_assists`) is
excluded as monotonic influence -- Riot's assist credit for these events
can be awarded on loose proximity/tick criteria, not a verified fight
contribution; `objective_fight_involvements` (spatially/temporally
causal-filtered by `score_features.py`) is used instead. Vision and
objective *influence* are only fed to the model through
`vision_influence.vision_actionable_rate` and `objective_participation`'s
secure/involvement counts, which are already causally filtered by
`score_features.py`.

Per-tier contracts (`TIER_FEATURE_CONTRACTS`): the four evidence tiers do
not all support the same features -- `aggregate` has no event timeline at
all, `lcu_timeline` has events but no ward events, and `live_client` has
events but neither ward events nor minute frames. Rather than including
every feature in one universal list and letting weaker tiers silently
carry permanently-absent features, each tier gets an explicit contract
built from only the `FeatureSpec`s whose `required_capability` that tier
actually supports. `aggregate`'s contract is intentionally just the three
always-available raw KDA counts -- it makes no claim of objective,
vision, or economy-conversion evidence at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from score_v2.leakage import validate_feature_payload

DIRECTION_POSITIVE = 1
DIRECTION_NEGATIVE = -1
DIRECTION_UNCONSTRAINED = 0
VALID_DIRECTIONS = (DIRECTION_POSITIVE, DIRECTION_NEGATIVE, DIRECTION_UNCONSTRAINED)

TRANSFORM_IDENTITY = "identity"
TRANSFORM_LOG1P = "log1p"
TRANSFORM_CLAMP01 = "clamp01"

_TRANSFORMS = {
    TRANSFORM_IDENTITY: lambda x: float(x),
    # log1p is monotonically increasing for x >= 0, so it compresses count
    # outliers (e.g. a 6-kill outlier game) without reversing feature order.
    TRANSFORM_LOG1P: lambda x: math.log1p(max(0.0, float(x))),
    TRANSFORM_CLAMP01: lambda x: min(1.0, max(0.0, float(x))),
}

# Capability labels describe WHY a feature can be structurally absent for a
# participant -- purely informative metadata (actual presence is always
# determined dynamically by walking `path`, since `score_features.py`
# already encodes real per-participant availability, e.g. a `match_v5` game
# can still have `resource_conversion.available = False` if no lane
# opponent could be identified). They ALSO define each tier's canonical
# feature contract -- see `TIER_FEATURE_CONTRACTS` below.
CAPABILITY_ALWAYS = "always"
CAPABILITY_EVENT_EVIDENCE = "event_evidence"
CAPABILITY_WARD_EVENTS = "ward_events"
CAPABILITY_MINUTE_FRAMES = "minute_frames"
CAPABILITY_LIVE_SNAPSHOTS = "live_snapshots"
VALID_CAPABILITIES = (
    CAPABILITY_ALWAYS, CAPABILITY_EVENT_EVIDENCE, CAPABILITY_WARD_EVENTS,
    CAPABILITY_MINUTE_FRAMES, CAPABILITY_LIVE_SNAPSHOTS,
)


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    path: tuple[str, ...]
    direction: int
    transform: str
    required_capability: str
    group: str
    description: str = ""

    def __post_init__(self) -> None:
        if self.direction not in VALID_DIRECTIONS:
            raise ValueError(f"{self.name}: invalid direction {self.direction!r}")
        if self.transform not in _TRANSFORMS:
            raise ValueError(f"{self.name}: unknown transform {self.transform!r}")
        if self.required_capability not in VALID_CAPABILITIES:
            raise ValueError(
                f"{self.name}: unknown capability {self.required_capability!r}"
            )
        if not self.path:
            raise ValueError(f"{self.name}: path must not be empty")

    def apply_transform(self, raw_value: float) -> float:
        return _TRANSFORMS[self.transform](raw_value)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": list(self.path),
            "direction": self.direction,
            "transform": self.transform,
            "required_capability": self.required_capability,
            "group": self.group,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "FeatureSpec":
        return cls(
            name=data["name"],
            path=tuple(data["path"]),
            direction=int(data["direction"]),
            transform=data["transform"],
            required_capability=data["required_capability"],
            group=data.get("group", "misc"),
            description=data.get("description", ""),
        )


def _spec(name, path, direction, transform, capability, group, description=""):
    return FeatureSpec(
        name=name, path=tuple(path), direction=direction, transform=transform,
        required_capability=capability, group=group, description=description,
    )


# Ordered, hand-reviewed feature allowlist. Order is part of the contract:
# `score_v2.artifact.Artifact.feature_specs` persists this exact order, and
# `score_v2.training.baseline` iterates it deterministically.
FEATURE_ALLOWLIST: tuple[FeatureSpec, ...] = (
    _spec("fight_kill_events", ("fight_influence", "kill_events"),
          DIRECTION_POSITIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "fight"),
    _spec("fight_death_events", ("fight_influence", "death_events"),
          DIRECTION_NEGATIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "fight"),
    _spec("fight_assist_events", ("fight_influence", "assist_events"),
          DIRECTION_POSITIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "fight"),
    _spec("fight_first_blood", ("fight_influence", "first_blood"),
          DIRECTION_POSITIVE, TRANSFORM_IDENTITY, CAPABILITY_EVENT_EVIDENCE, "fight"),
    _spec("fight_untraded_deaths", ("fight_influence", "untraded_deaths"),
          DIRECTION_NEGATIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "fight"),
    _spec("fight_kill_participation", ("fight_influence", "event_kill_participation"),
          DIRECTION_POSITIVE, TRANSFORM_CLAMP01, CAPABILITY_EVENT_EVIDENCE, "fight"),
    _spec("objective_epic_secures", ("objective_participation", "epic_monster_secures"),
          DIRECTION_POSITIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "objective"),
    _spec("objective_grub_secures", ("objective_participation", "grub_secures"),
          DIRECTION_POSITIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "objective"),
    _spec("objective_fight_involvements",
          ("objective_participation", "objective_fight_involvements"),
          DIRECTION_POSITIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "objective",
          "Spatially/temporally causal-filtered fight support around an "
          "objective -- NOT a raw Riot monster-kill assist credit."),
    _spec("objective_turret_kills", ("objective_participation", "turret_kills"),
          DIRECTION_POSITIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "objective"),
    _spec("objective_turret_assists", ("objective_participation", "turret_assists"),
          DIRECTION_POSITIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "objective"),
    _spec("objective_turret_plates", ("objective_participation", "turret_plates"),
          DIRECTION_POSITIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "objective"),
    _spec("objective_inhibitor_kills", ("objective_participation", "inhibitor_kills"),
          DIRECTION_POSITIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "objective"),
    _spec("structure_secures", ("structure_pressure", "structure_secures"),
          DIRECTION_POSITIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "structure"),
    _spec("enablement_ally_assists", ("enablement_suppression", "ally_enablement_assists"),
          DIRECTION_POSITIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "enablement"),
    _spec("enablement_suppression_weight",
          ("enablement_suppression", "suppression_weight"),
          DIRECTION_POSITIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "enablement"),
    _spec("vision_actionable_rate", ("vision_influence", "vision_actionable_rate"),
          DIRECTION_POSITIVE, TRANSFORM_CLAMP01, CAPABILITY_WARD_EVENTS, "vision",
          "Only actionable wards/dewards -- never raw vision_score."),
    _spec("death_tempo_count", ("death_tempo", "death_count"),
          DIRECTION_NEGATIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "tempo"),
    _spec("death_tempo_rapid_pairs", ("death_tempo", "rapid_death_pairs"),
          DIRECTION_NEGATIVE, TRANSFORM_LOG1P, CAPABILITY_EVENT_EVIDENCE, "tempo"),
    _spec("resource_conversion_rate", ("resource_conversion", "conversion_rate"),
          DIRECTION_POSITIVE, TRANSFORM_CLAMP01, CAPABILITY_MINUTE_FRAMES, "economy"),
    _spec("raw_kills", ("raw", "kills"),
          DIRECTION_POSITIVE, TRANSFORM_LOG1P, CAPABILITY_ALWAYS, "raw"),
    _spec("raw_deaths", ("raw", "deaths"),
          DIRECTION_NEGATIVE, TRANSFORM_LOG1P, CAPABILITY_ALWAYS, "raw"),
    _spec("raw_assists", ("raw", "assists"),
          DIRECTION_POSITIVE, TRANSFORM_LOG1P, CAPABILITY_ALWAYS, "raw"),
    _spec("live_dead_sample_rate", ("live_state", "dead_sample_rate"),
          DIRECTION_NEGATIVE, TRANSFORM_CLAMP01, CAPABILITY_LIVE_SNAPSHOTS, "live"),
)

FEATURE_NAMES: tuple[str, ...] = tuple(spec.name for spec in FEATURE_ALLOWLIST)

# Raw stats deliberately withheld from the allowlist above, with why --
# enforced by `tests/test_score_v2_feature_spec.py`.
EXCLUDED_RAW_FIELDS: Mapping[str, str] = {
    "vision_score": "raw vision score is not vision influence (Seraphine v1 regression)",
    "wards_placed": "ward count without conversion is not influence",
    "wards_killed": "deward count without conversion is not influence",
    "damage_to_turrets": "turret damage share is not a secure/assist (Vel'Koz v1 regression)",
    "damage_to_objectives": "objective damage share is not a secure/assist",
    "damage_to_champions": "raw damage output is not fight influence (no trade/context)",
    "gold_earned": "raw gold without conversion evidence is not causally validated influence",
    "cs": "raw CS without conversion evidence is not causally validated influence",
}

# Monster-kill "assist" fields deliberately withheld as monotonic
# influence -- Riot's assist credit for these events is looser than a
# verified fight contribution. `objective_fight_involvements` (a
# spatially/temporally causal-filtered signal) is used instead. Kept
# separate from EXCLUDED_RAW_FIELDS because these live under
# `objective_participation`, not `raw`.
EXCLUDED_OBJECTIVE_ASSIST_FIELDS: Mapping[str, str] = {
    "epic_monster_assists": (
        "raw monster-kill assist credit is not verified fight influence -- "
        "see objective_fight_involvements"
    ),
    "grub_assists": (
        "raw monster-kill assist credit is not verified fight influence -- "
        "see objective_fight_involvements"
    ),
}

# Which of the four evidence tiers structurally support which
# capabilities -- see the module docstring "Per-tier contracts". Ordered
# tuples, not sets, so contract membership stays deterministic.
_TIER_CAPABILITY_SUPPORT: Mapping[str, tuple[str, ...]] = {
    "match_v5": (
        CAPABILITY_ALWAYS, CAPABILITY_EVENT_EVIDENCE, CAPABILITY_WARD_EVENTS,
        CAPABILITY_MINUTE_FRAMES,
    ),
    "lcu_timeline": (CAPABILITY_ALWAYS, CAPABILITY_EVENT_EVIDENCE, CAPABILITY_MINUTE_FRAMES),
    "live_client": (CAPABILITY_ALWAYS, CAPABILITY_EVENT_EVIDENCE, CAPABILITY_LIVE_SNAPSHOTS),
    "aggregate": (CAPABILITY_ALWAYS,),
}


def _build_tier_contract(supported_capabilities: Sequence[str]) -> tuple[FeatureSpec, ...]:
    supported = frozenset(supported_capabilities)
    return tuple(spec for spec in FEATURE_ALLOWLIST if spec.required_capability in supported)


# One canonical, immutable feature contract per evidence tier. `aggregate`
# ends up with only the three always-available raw KDA counts -- no
# objective/vision/economy-conversion claim at all. `score_v2.artifact`
# requires a loaded artifact's coefficients to match its tier's contract
# EXACTLY (same names, same spec content) -- see
# `Artifact.validate`/`tests/test_score_v2_artifact.py`.
TIER_FEATURE_CONTRACTS: Mapping[str, tuple[FeatureSpec, ...]] = {
    tier: _build_tier_contract(capabilities)
    for tier, capabilities in _TIER_CAPABILITY_SUPPORT.items()
}


def feature_contract_for_tier(evidence_source: str) -> tuple[FeatureSpec, ...]:
    try:
        return TIER_FEATURE_CONTRACTS[evidence_source]
    except KeyError:
        raise ValueError(f"Unknown evidence_source {evidence_source!r}") from None


def _walk_path(block: Any, path: Sequence[str]) -> Any:
    node = block
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


@dataclass(frozen=True)
class FeatureValue:
    name: str
    raw: Optional[float]
    transformed: Optional[float]
    present: bool


def extract_raw_value(
        participant_features: Mapping, spec: FeatureSpec) -> Optional[float]:
    node = _walk_path(participant_features, spec.path)
    if node is None:
        return None
    if isinstance(node, bool):
        return 1.0 if node else 0.0
    if isinstance(node, (int, float)):
        return float(node)
    return None


def extract_feature_value(
        participant_features: Mapping, spec: FeatureSpec) -> FeatureValue:
    raw = extract_raw_value(participant_features, spec)
    if raw is None:
        return FeatureValue(name=spec.name, raw=None, transformed=None, present=False)
    return FeatureValue(
        name=spec.name, raw=raw, transformed=spec.apply_transform(raw), present=True,
    )


def extract_feature_vector(
        participant_features: Mapping,
        specs: Sequence[FeatureSpec] = FEATURE_ALLOWLIST,
) -> dict[str, FeatureValue]:
    """Extract every allowlisted feature from one participant's block.

    Validates `participant_features` for outcome leakage before reading
    anything out of it -- this is the one required entry point both
    training-dataset construction and the runtime scorer must call.
    """
    validate_feature_payload(participant_features)
    return {spec.name: extract_feature_value(participant_features, spec) for spec in specs}


def resolve_role(participant_features: Mapping) -> str:
    role = _walk_path(participant_features, ("baseline", "role"))
    return str(role) if role else "unknown"


def resolve_champion(participant_features: Mapping) -> Optional[str]:
    champion = _walk_path(participant_features, ("baseline", "champion"))
    return str(champion) if champion else None
