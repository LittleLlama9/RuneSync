"""DAEMON Score v2 model training/evaluation/runtime-artifact pipeline.

This package sits strictly downstream of `score_features.py` (per-game,
per-participant evidence extraction) and the `corpus/` package (manifest,
splits, blinded pairwise review). It never talks to a Riot endpoint, a
`HistoryStore` write path, or the UI/bridge/coaching layers.

Layering (runtime vs. development), enforced by import discipline rather
than just convention:

  * `score_v2.leakage` / `score_v2.feature_spec` / `score_v2.artifact` /
    `score_v2.runtime` are the **shipped runtime path**. They import only
    the Python standard library (`math`, `json`, `hashlib`, `dataclasses`,
    `typing`) -- never `numpy`, `scipy`, or `sklearn` -- so a packaged
    RuneSync build can score a match with nothing beyond the interpreter.
  * `score_v2.training.*` is **development-only** tooling (invoked from
    `scripts/score_v2/*.py`, never imported by `app.py`/`bridge.py`). It is
    also written stdlib-only on purpose: the current corpus is tiny, so a
    deterministic hand-rolled regularized linear/pairwise trainer is both
    sufficient and exactly reproducible in tests, and it avoids adding a
    numpy/scipy/sklearn dependency (`requirements.txt` has none today) for
    a training path that isn't even producing a production artifact yet.

No stage in this package fabricates a human review label, a production
calibration, or a "ready to replace v1" claim. See
`docs/SCORE_V2_MODELS.md` and `docs/SCORE_V2_MODEL_CARD_TEMPLATE.md`.
"""

from __future__ import annotations

FEATURE_ALLOWLIST_SCHEMA_VERSION = 1
