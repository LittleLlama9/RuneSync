# DAEMON Score v2 synthetic review panel

The synthetic panel creates provisional, outcome-blind training labels for the
opt-in personal beta. It does not establish human-expert accuracy and is not a
production release gate substitute.

## Workflow

1. RuneSync exports every newest exact-tier feature set into two independently
   shuffled match rounds.
2. Public packets contain opaque subject IDs, anonymous team labels, champion,
   role, evidence completeness, and the existing outcome-free Score v2 feature
   blocks. They omit game IDs, participant IDs, player names, PUUIDs, win/loss,
   DAEMON scores, ranks, and companion scores. Necessary cross-player
   references such as a lane opponent are remapped to the other export-local
   subject ID.
3. Three independent judge models grade each round from the same immutable,
   literature-grounded rubric.
   Each model/round combination runs in a separate context. A judge must never
   receive both shuffled rounds because identical champion and evidence values
   would allow it to recognize and copy its earlier ranking.
4. Every judge returns ranking tiers, ties where appropriate, confidence,
   controlled rationale tags, and dotted evidence paths that must exist in the
   packet.
5. A reviewer comparison counts only pairwise relations that remain identical
   across both shuffled rounds.
6. The aggregator emits existing `corpus.review` labels only when at least
   three stable reviewers agree unanimously and every supporting confidence is
   at least `0.65`. By default it keeps only comparisons within three ranking
   tiers. Wider comparisons can be enabled explicitly, but the resulting
   pairwise count must not be presented as independent evidence.

Judgments and private token maps remain local under `%APPDATA%\RuneSync\corpus`.
Synthetic reviewer IDs are explicitly prefixed with `synthetic:` so they cannot
be confused with future human labels.

One ten-player ranking expands into up to 45 correlated pairwise relations.
The aggregator emits one consensus label per accepted relation rather than one
row per judge. Training evaluation must remain grouped by match and must not
describe the resulting pair count as 45 independent matches.

## Export

```powershell
py scripts\score_v2\run_synthetic_panel.py export `
  --output-dir "$env:APPDATA\RuneSync\corpus\synthetic-panel"
```

The directory contains:

- `rubric.json`
- `judge-prompt.txt`
- `round-a.jsonl`
- `round-b.jsonl`
- `private-map.json`

The round files and prompt may be given to judge agents. `private-map.json`
must never be shown to a judge. Public packets are pseudonymous, not anonymous:
random export-local subject IDs prevent direct ID reconstruction, but exact
champion rosters, duration, and structured telemetry may still fingerprint a
match. Do not send packets to an external model provider without the data
owner's explicit consent and an acceptable provider privacy policy.

## Judge output

Each judge writes one JSON object per input packet:

```json
{
  "packet_id": "opaque id",
  "ranking_tiers": [["subject-a"], ["subject-b", "subject-c"]],
  "assessments": [
    {
      "subject_id": "subject-a",
      "confidence": 0.84,
      "rationale_tags": ["combat_impact", "economy_efficiency"],
      "evidence_paths": [
        "fight_influence.kill_events",
        "resource_conversion.conversion_rate"
      ],
      "brief_reason": "Converted a lead into repeated contextual influence."
    }
  ],
  "overall_confidence": 0.78,
  "abstain_reason": ""
}
```

An abstention has empty `ranking_tiers` and `assessments` plus a non-empty
`abstain_reason`.

## Aggregate

```powershell
py scripts\score_v2\run_synthetic_panel.py aggregate `
  --input-dir "$env:APPDATA\RuneSync\corpus\synthetic-panel" `
  --judgment claude=a=claude-a.jsonl `
  --judgment claude=b=claude-b.jsonl `
  --judgment gpt=a=gpt-a.jsonl `
  --judgment gpt=b=gpt-b.jsonl `
  --judgment gemini=a=gemini-a.jsonl `
  --judgment gemini=b=gemini-b.jsonl `
  --max-tier-gap 9 `
  --labels-output synthetic-labels.jsonl `
  --token-map-output synthetic-token-map.json `
  --report-output synthetic-panel-report.json
```

The normal `build_training_dataset.py` CLI can consume the resulting labels and
token map. Training and validation must continue to keep synthetic provenance
visible and must not describe these labels as human ground truth.

Because one personal archive contains the same local player in every match, the
strict player-disjoint splitter correctly treats the archive as one connected
component. An opt-in personal beta may instead use:

```powershell
py scripts\score_v2\build_training_dataset.py `
  --split-strategy temporal-personal ...
```

This keeps whole games together and uses the oldest games for training and the
newest games for testing. It deliberately allows the same local player across
splits and therefore cannot support public/general performance claims. Release
artifacts must continue to use the default `strict-grouped` strategy.

## First personal beta run

The first run graded all 53 retained games in two shuffled rounds with three
independent model families. All six 53-row judgment files passed strict packet,
subject, evidence-path, and leakage validation.

- 1,318 unanimous, order-stable, confidence-qualified pair labels were retained
  with `--max-tier-gap 9`.
- Hard-cut sensitivity runs at tier gaps 3 and 4 retained 304 and 558 labels,
  respectively, but materially reduced match-grouped ranking agreement. The
  personal beta therefore retains the full stable ranking constraints and
  reports match-grouped NDCG, Spearman, exact-rank, and within-one-rank metrics
  alongside pairwise accuracy.
- The selected linear model converged after 1,335 of 2,000 configured
  iterations. It is a development-only personal artifact, not a Score v1
  replacement or a public accuracy claim.
- The chronological test block was inspected during label-strategy
  sensitivity analysis. It is no longer an untouched final holdout. Future
  newly captured games must supply the next honest personal evaluation.

The wider tier-gap choice is specific to this provisional run. It is not a
general recommendation and must be revisited on a fresh holdout.

## External research data

PandaSkill's licensed professional-play release is suitable for methodology,
role-normalization, and panel-comparison experiments. It contains 37,459
professional games, 374,554 player rows, and 1,884,740 event rows from
September 2019 through September 2024, plus published performance-score,
engineered-feature, and expert-survey files.

It is not representative solo queue and lacks the full minute-position
timelines used by RuneSync's local LCU evidence tier. Keep it in a separate
source/domain adapter with explicit provenance; do not merge it into personal
LCU training records without a domain marker. Fresh representative solo-queue
timelines still require Riot Match-V5 access. Scraped companion-site data and
unverified redistributed Riot timelines are not accepted sources.

## Research boundary

The rubric is grounded in contextual individual-impact work such as SIDO and
PandaSkill, encounter detection, and documented LLM-judge bias mitigations.
AlphaStar and OpenAI Five contribute only the concepts of state value and
long-horizon credit assignment; they are not League grading systems.

The panel reduces single-model opinion and presentation-order bias. It cannot
prove correctness, eliminate correlated model errors, or establish fairness
across every role, champion, rank, and playstyle. Public promotion still
requires independent human validation.
