# DAEMON Score v2 release audit

`scripts\score_v2\audit_release.py` is the final fail-closed boundary between a
reviewed candidate artifact and any human decision to mark a new artifact
`production_ready=true`.

```powershell
py scripts\score_v2\audit_release.py `
  --artifact artifacts\candidate\lcu_timeline.json `
  --evidence release-evidence\lcu_timeline.json `
  --output release-evidence\audit.json
```

The evidence manifest must identify the artifact exactly and contain each
required gate once:

- `match_v5_verification`
- `human_pairwise_labels`
- `actual_training_leakage_scan`
- `adversarial_cases`
- `calibration_and_bootstrap`
- `coaching_human_acceptance`
- `fairness_drift_and_external_benchmark`
- `collector_and_packaged_runtime`
- `independent_artifact_review`
- `release_scope_and_todos`

Every gate needs a non-empty summary. A `passed` gate also needs a non-empty
evidence file beneath the manifest directory and its exact SHA-256 hash.
Missing, duplicate, unknown, path-escaping, empty, or hash-mismatched evidence
fails the audit.

Exit codes:

- `0`: every gate passed;
- `1`: malformed artifact, manifest, or evidence;
- `2`: one or more gates are blocked or failed;
- `3`: the artifact is already marked production-ready while a gate is not
  passed.

Passing does not modify the artifact. A development artifact with every gate
passed is reported as `ready_for_human_promotion`; a separately rebuilt,
independently reviewed artifact may then set `production_ready=true`. The
pipeline never flips that field automatically.

The audit proves bundle completeness, artifact identity, and evidence-file
integrity. It does not decide whether a metric or human judgment is correct;
that responsibility remains with the independent review named by each gate.
