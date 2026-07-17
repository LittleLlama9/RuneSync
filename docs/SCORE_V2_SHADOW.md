# DAEMON Score v2 shadow reports

Shadow mode compares an explicit Score v2 artifact with the newest retained
Score v1 run without changing the score shown by RuneSync.

```powershell
py scripts\score_v2\run_shadow.py `
  --history-db "$env:APPDATA\RuneSync\history.db" `
  --artifacts-dir artifacts\reviewed `
  --allow-development-artifacts `
  --backfill-features `
  --output shadow-report.json
```

The command:

- selects only exact-tier artifacts using the runtime router;
- rejects neutral `insufficient_data` artifacts;
- optionally extracts and persists canonical feature sets from retained
  Match-V5, LCU, Live Client, or aggregate evidence;
- scores in memory without writing `score_runs` or changing
  `matches.active_score_run_id`;
- compares all non-abstained participants with the newest immutable v1 run;
- reports score MAE/correlation, exact and within-one rank agreement,
  abstention coverage, local-player divergence, evidence coverage, and
  artifact provenance; and
- checks captured verified adversarial cases when their games are present.

Participant rows contain only the local database participant ID, champion,
role, scores, ranks, and uncertainty fields. Summoner names and PUUIDs are not
written to the report.

Without `--artifacts-dir`, the same command produces an evidence inventory.
This is the honest current mode while no reviewed artifact exists:

```powershell
py scripts\score_v2\run_shadow.py `
  --history-db "$env:APPDATA\RuneSync\history.db" `
  --backfill-features `
  --output evidence-inventory.json
```

Every report sets `release_eligible` to `false`. Shadow output is evidence for
the later validation/release review; it is never itself release authorization.
