# DAEMON Score v2 Corpus, Splits, and Blinded Review

This document covers the tooling under `corpus/`: the corpus manifest
format, grouped train/validation/test split assignment with leakage
checks, the adversarial case library, and the blinded pairwise review
workflow. This is corpus/labeling infrastructure only -- it does not touch
feature extraction, model training, routing, UI, or credential/provider
code (those are owned separately).

## Scope and honesty principles

- **No raw credentials or Riot API keys are ever written to a manifest,
  case, or review artifact.** `corpus/_privacy.py`'s `scan_for_forbidden`
  is run at every construction/validation boundary (manifest entries,
  adversarial cases, review labels) and raises rather than silently
  dropping a problem.
- **No raw PUUID or summoner name ever leaves `history_store.py`.**
  `corpus/manifest.hash_identifier()` salts and SHA-256-hashes every player
  identifier into a local `p_<24 hex>` id before it reaches a manifest
  entry. The review tool's presentation allowlist
  (`corpus/review.ALLOWED_PRESENTATION_FIELDS`) independently guarantees a
  reviewer never sees `puuid`, `summoner_name`, `win`, `total_score`,
  `match_rank`, `team_id`, `participant_id`, or `game_id`.
- **Unavailable evidence is never fabricated.** If a requested evidence
  source has no stored payload for a game (e.g. no `match_v5` timeline
  because bulk Match-V5 acquisition is currently authorization-blocked),
  `corpus/build_from_history.py` raises
  `HistoryEvidenceUnavailableError` instead of inventing a placeholder.
  Region and rank/tier are honestly recorded as unknown (with a
  `*_unknown_reason`) because `history_store.py`'s schema does not capture
  them at all -- this tooling does not guess.
- Bulk/authorized Match-V5 acquisition remains covered by a separate,
  currently-blocked verification gate (see `docs/RIOT_API_KEY_POLICY.md`).
  This corpus tooling is fully usable today with local LCU
  timeline/aggregate evidence, and with Match-V5 personal-key research
  data if/when it exists locally; it does not depend on that gate being
  unblocked.

## Corpus manifest (`corpus/manifest.py`)

Each `ManifestEntry` (`entry_id = "{game_id}:{source}"`) records:

- **Provenance**: capture method, collection tool/version, source schema
  version, capture timestamp, free-text notes.
- **Schema versions**: `MANIFEST_SCHEMA_VERSION` (manifest format) and
  per-entry `source_schema_version` (the underlying evidence's schema).
- **Content hash**: `content_hash` is a pure function of the entry's facts
  (game id, source, capability flags, completeness, game metadata,
  leakage keys, and -- when available -- the real
  `HistoryStore.get_timeline_payload()` content hash), explicitly
  excluding volatile timestamps. Building the same entry twice from the
  same facts always yields the same `content_hash`.
- **Capability flags**: 11 booleans (`has_minute_frames`,
  `has_subminute_resolution`, `has_victim_damage_detail`,
  `has_ward_events`, `has_item_events`, `has_building_events`,
  `has_champion_events`, `has_bounty_detail`, `has_position_timeline`,
  `has_all_player_parity`, `has_local_player_detail`) with honest
  per-source defaults reflecting the real capability gaps between
  Match-V5, LCU timeline, Live Client, and aggregate evidence.
- **Completeness**: a `[0.0, 1.0]` float.
- **Privacy classification**: `local_hashed_real` or
  `local_hashed_synthetic`.
- **Consent status**: `personal_local_client_data`,
  `match_v5_personal_key_research_only`,
  `match_v5_authorization_blocked`, `synthetic_no_consent_required`, or
  `unknown`.
- **Game metadata**: patch, queue, map, duration, creation date, and
  honestly-`None` region/rank-tier with an explicit unknown-reason.
- **Leakage keys**: `match_group_key`, `player_group_keys` (hashed),
  champion, role, region, rank tier, patch -- the inputs to split
  assignment and leakage checking.

`CorpusManifest` is a file-backed, deterministically-serialized (sorted
keys, sorted `entry_id`) collection: `add_entry()` is idempotent for
identical facts, raises on conflicting facts under the same `entry_id`
unless `allow_overwrite=True` is passed explicitly, and `save()`/`load()`
round-trip through JSON.

Evidence sources: `match_v5`, `lcu_timeline`, `live_client`, `aggregate`
(matching the existing `history_store.py`/`timeline_provider.py`
conventions; `live_client` is a new but consistent addition since
`live_client.py` does not currently register a `timeline_payloads.source`
string of its own).

### Building entries from the local store (`corpus/build_from_history.py`)

`build_entry_from_history(store, game_id, source, identity_salt=...)`
reads a `HistoryStore` (read-only: `get_report`, `get_timeline_payload`,
`list_live_capture_sessions`) and returns a sanitized `ManifestEntry`, or
raises `HistoryEvidenceUnavailableError` if that source was never
captured for that game. `available_sources_for_game(store, game_id)`
reports which sources genuinely have stored data, for building a manifest
without guessing.

## Grouped splits and leakage checks (`corpus/splits.py`)

`assign_splits(entries, SplitConfig(seed=..., ratios={...}))` unions each
match's `match_group_key` with every one of its `player_group_keys` via a
deterministic union-find, then assigns each whole connected component to
train/validation/test using a seeded, reproducible hash bucket. **If a
player appears in two different matches, both matches (and everyone else
in them) land in the same split** -- a strict reading of "same
player/match must never cross groups."

`check_leakage(entries, assignments)` is an independent re-verification
(it does not trust `assign_splits`'s internals) producing a
`LeakageReport`:

- **Hard violations** (`is_clean()` is `False` if any exist): a player
  group spanning multiple splits, a match group spanning multiple splits,
  or two entries with an identical `content_hash` (the same underlying
  evidence registered twice) landing in different splits.
- **Warnings** (non-fatal): a champion, region, rank tier, or patch value
  concentrated entirely into one split, or temporally-adjacent entries
  (within `temporal_window_seconds`, default 300s) split apart.

`assign_splits_strict()` combines both steps and raises
`SplitLeakageError` if any hard violation is found. Seed and ratio
version metadata (`SplitConfig.seed`/`.version`) travel with every
assignment for reproducibility.

## Adversarial case library (`corpus/adversarial_cases.py`,
`corpus/data/adversarial_cases.json`)

11 cases across the 10 required categories (tank, support, split_pusher,
weak_side_play, short_game, low_kda_influence, vision_without_conversion,
raw_economy_without_influence, objective_contact_without_contest,
disputed_score). Two are `verified_local` (grounded in real, locally
sanitized DB facts); the rest are clearly-labeled `synthetic`.
Each `verified_local` case carries explicit `evidence_provenance`. The
K'Sante dispute combines sanitized database rows with the authoritative LCU
match-details and 33-frame timeline responses captured during the original
investigation; it is not inferred from the reduced legacy database schema.

- **`verified-5601631110-short-game-insufficient-evidence`** (game
  5601631110, 8:30/510s): the model must abstain
  (`insufficient_evidence`) rather than confidently rank a Sion who
  "won" a match that ended almost immediately.
- **`verified-5602827182-ksante-seraphine-velkoz`** (game 5602827182,
  ~31:01): a compound expectation --
  1. K'Sante (8/7/4, 21543 damage to champions, losing team) must beat
     Seraphine (3/15/14, vision_score 79) despite Seraphine's v1 score
     currently ranking above K'Sante's -- the exact regression this case
     guards against.
  2. K'Sante must also beat Vel'Koz (9/12/7,
     `damage_to_turrets` 5609, the losing team's highest), because the
     verified timeline gives K'Sante first blood, two turret kills, two
     grub events, and a sustained lane lead while Vel'Koz had only one
     direct structure assist and 12 deaths.

`evaluate_case(case, scores=..., duration_seconds=...)` returns an
`EvaluationResult` with `passed` in `{True, False, None}` --
`None` means "cannot resolve" (missing data, or a genuinely
`disputed_manual_review` case), never a fabricated verdict.
Expectation types: `insufficient_evidence`, `pairwise_minimum_gap`
(supports a negative `min_gap` for "must stay roughly competitive" cases
like weak-side play, not just "must strictly outscore"),
`must_not_rank_first_solely_on_metric`, `disputed_manual_review`, and
`compound` (nested sub-expectations, each independently evaluated and
reported in `sub_results`).

## Blinded pairwise review (`corpus/review.py`)

- **Blinding**: `redact_for_presentation()` copies only fields in
  `ALLOWED_PRESENTATION_FIELDS` (champion, role, level, KDA, gold, cs,
  damage/vision/ward stats, duration, patch) -- everything else is
  dropped by omission from an allowlist, not hidden by a denylist, with a
  defense-in-depth `scan_for_forbidden` check as a backstop.
- **Randomized left/right**: `build_presentation(item_a, item_b, seed=...)`
  seeds a deterministic coin flip on `f"{seed}:{pair_id}"` so the same
  pair with the same seed always renders the same way, but different
  pairs/seeds are independently randomized. The reviewer only ever sees
  opaque hashed tokens, never the real `item_ref`; the real mapping
  (`{"left_token", "right_token", "left_ref", "right_ref"}`) is returned
  separately as private bookkeeping the caller must store apart from
  anything reviewer-facing.
- **Reviewer choices**: `left`, `right`, `tie`, or
  `insufficient_evidence`, each with a `confidence` in `[0.0, 1.0]`, at
  least one `rationale_tag` from a 10-tag controlled vocabulary, and an
  optional free-text `notes` (privacy-scanned).
- **Append-only labels**: `ReviewLabelStore` only ever appends a JSONL
  line (`add_label`); there is no update/delete method. A correction is
  recorded as a new label, preserving the full label history as an audit
  trail. Malformed lines are skipped (collected into
  `store.last_load_errors`) rather than crashing the reader.
- **Inter-rater agreement**: `compute_agreement()` reports unanimity rate
  across all multi-rater pairs, plus Cohen's kappa when exactly 2 distinct
  reviewers are present in the label set (`None` for 1 or 3+ reviewers --
  an honest limitation rather than a possibly-misleading generalized
  statistic).
- **Export for training**: `export_for_training(labels, token_maps)`
  de-blinds every label (not deduplicated per pair) into a training-ready
  row with `winner_ref`/`relation` resolved via the private token map;
  `on_missing_mapping="raise"|"skip"` controls behavior when a label's
  pair has no stored mapping.

## Privacy: retention and deletion

- The corpus directory (`corpus.manifest.default_corpus_dir()`) lives at
  `%APPDATA%\RuneSync\corpus\`, never inside the repository.
- Player pseudonymization uses a locally-stored salt
  (`load_or_create_identity_salt()`). **Rotating or deleting that salt
  file is the "forget" mechanism**: every hashed id derived from it
  becomes unrecoverable and unlinkable to future re-hashes of the same
  real PUUID.
- Raw Match-V5 payloads are never published or copied into a manifest;
  only a real SHA-256 content hash of the stored payload is folded into
  `content_hash`, so a manifest entry's identity is tied to genuine
  evidence without ever re-exposing the payload itself.
- Review labels are append-only; deleting a reviewer's contribution
  (e.g. on request) means deleting their `reviewer_id`'s rows from the
  JSONL file directly (an operational, not code-level, deletion path) --
  this is intentionally outside the append-only API to keep normal
  operation tamper-evident.

## CLI entry points

Each module exposes a stdlib-only `argparse` CLI for non-interactive use:

```
py -m corpus.manifest validate <manifest.json>
py -m corpus.manifest stats <manifest.json>
py -m corpus.splits <manifest.json> --seed 42
py -m corpus.adversarial_cases validate
py -m corpus.adversarial_cases list [--category short_game]
py -m corpus.review present <item_a_ref> <item_a_features.json> <item_b_ref> <item_b_features.json> --seed <seed> --token-map-out <token_map.json>
py -m corpus.review label <labels.jsonl> --pair-id <id> --reviewer-id <id> --choice left --confidence 0.8 --tags combat_impact
py -m corpus.review agreement <labels.jsonl>
py -m corpus.review export <labels.jsonl> <token_map.json>
```

## Tests

`tests/test_corpus_manifest.py`, `tests/test_corpus_splits.py`,
`tests/test_adversarial_cases.py`, `tests/test_corpus_review.py`, and
`tests/test_build_from_history.py` cover deterministic
manifest/split construction, leakage rejection (including forced
player/match-spanning and duplicate-content-hash scenarios), blinding
(allowlist enforcement even when forbidden fields are present in the
source), append-only label behavior, malformed-input handling, agreement
calculation, and both verified adversarial cases (5601631110,
5602827182). `tests/fixtures/corpus/manifest_sample.json` is a small,
deterministic 4-source sample manifest used by the splits round-trip
test.
