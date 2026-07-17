# DAEMON Score v2 routing

**Status: integrated, opt-in beta, inactive by default.** RuneSync ships no
trained Score v2 artifact yet. The retained v1 scorer remains the active
fallback until the user enables the beta and a valid exact-tier artifact is
installed.

## Artifact loading

When **DAEMON Score v2 beta** is enabled in Settings, RuneSync checks at
startup:

```text
%APPDATA%\RuneSync\score-v2-artifacts\
```

for exact-tier files named `match_v5.json`, `lcu_timeline.json`,
`live_client.json`, and `aggregate.json`. A present artifact must:

- pass content-hash and schema validation;
- declare the same evidence tier as its filename;
- use that tier's canonical feature contract; and
- contain a fitted signal rather than an `insufficient_data` neutral prior.

The beta may load a development artifact without `production_ready=true`, but
that does not promote or publish it. Any invalid or neutral artifact is
rejected and logged. A missing directory or missing tier file is normal and
leaves v1 active. Disabling the setting prevents artifacts from loading; a
restart applies changes to the runtime router.

## Route selection

`performance_score.ScoreRouter` receives only Score v2 feature sets. It never
reads the match result and never substitutes a model trained for another
evidence tier.

Among tiers that are both captured and backed by a registered artifact,
selection is completeness-aware. A more complete local source may beat a
partial richer source; source priority breaks equal-quality ties. Aggregate is
the explicit last fallback.

## Immutable upgrades

Each successful v2 score is appended as a new `score_runs` row with:

- evidence, feature, calibration, artifact, and model-family provenance;
- the exact feature-set input hash;
- participant score interval and confidence;
- group rank confidence; and
- abstention state and reasons.

The original v1 run remains stored. A stronger evidence tier may become active
later, such as a Match-V5 timeline arriving after an LCU score. A weaker rerun
is retained for provenance but cannot replace a stronger active result.

LCU ingestion, Match-V5 persistence, and reconciled Live Client capture all
trigger the same routing path. Failures are logged and leave the current active
run unchanged.

## Release boundary

Routing integration does not make Score v2 production-ready. Authorized
Match-V5 cross-source verification, real blinded-label calibration, shadow
comparison, coaching/UI work, and the remaining release gates must still pass
before RuneSync ships or enables a production artifact.
