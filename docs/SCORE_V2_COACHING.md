# DAEMON Score v2 selective coaching

Score v2 coaching is a separate eligibility layer, not a generic explanation
of the model score. It uses persisted causal features and signed timeline
events, and it abstains whenever the evidence cannot support a useful claim.

## Observations

Participant observations prefer timestamped facts such as:

```text
09:00 - Secured an epic objective.
12:00 - Died; the timeline records the event without inferring intent.
```

They do not describe a generic strongest/weakest score component and do not
infer intent. Aggregate-only games disclose that no participant-level event
timeline was available.

## Eligibility gates

Actionable coaching is withheld when any of these apply:

- the score itself abstained;
- participant confidence is below `0.65`;
- timeline/live evidence completeness is below `0.70`;
- only aggregate evidence is available; or
- no controllable negative pattern appears in at least two comparable games.

Comparable history is restricted to prior games, the same role, the same
evidence tier, and the same feature version. Future games are never read while
backfilling an older match, and abstained historical games do not establish a
pattern.

## Curated focus rules

The current rule set can select at most one primary focus:

- untraded deaths;
- rapid repeated deaths;
- unconverted observable lane leads;
- objective assist contact without nearby fight influence; or
- vision events without spatially and temporally linked allied follow-up.

Rules use direct feature thresholds and are deterministic. They do not treat
raw gold, CS, damage, vision score, or objective contact as automatic
influence.

## Challenges and anti-gaming

An eligible focus produces one measurable challenge targeting success in
three of the next five comparable games. Every challenge includes:

- the exact target;
- the feature-level measurement;
- the eligible-game condition where needed; and
- an anti-gaming guardrail.

Examples explicitly reject passive KDA preservation, forced low-value fights,
unsafe ward volume, or abandoning lane merely to satisfy a counter.

## Persistence

The active score result exposes:

- factual `observations`;
- `coaching_eligible`;
- `primary_focus`;
- challenge and recurrence metadata; and
- explicit `withheld_reasons`.

This stage stores the data for both Standard and Classic report/history
workflows. Presentation changes remain part of the Score v2 UI stage.
