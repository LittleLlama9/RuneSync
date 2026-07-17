# Riot API key policy (private research feature)

This document covers how RuneSync stores and uses a Riot Games API key for
the **optional, disabled-by-default** Match-V5 timeline research feature
(`RiotMatchV5Provider` in `timeline_provider.py`), and what Riot's own rules
require. It does not cover the League Client API (LCU) integration used for
rune import and normal match history, which needs no Riot API key at all and
is unaffected by anything in this document.

## Two different kinds of Riot API key

Riot's Developer Portal (https://developer.riotgames.com) issues two very
different kinds of key. Confusing them is the most common cause of a
surprise `403`.

| | Personal / Development key | Production key |
|---|---|---|
| Issued | Automatically on login, from your account dashboard | Only after Riot approves a registered product |
| Expiration | **24 hours**, then requests start failing until you regenerate it | No fixed expiration; stays valid while your product stays compliant |
| Renewal | Manual: log in to the portal and regenerate | N/A (persists) |
| Rate limits | Low (roughly 20 req/s, 100 req/2min) | Higher, set per approved application |
| Intended use | Personal projects, prototyping, local development/testing | Public/shipped products, per Riot's product registration process |
| Sharing/redistribution | Must not be shared or embedded in a distributed binary | Must be registered to one product; also must not be embedded in a distributed binary |

Sources: Riot Developer Portal application/portal docs
(https://developer.riotgames.com/docs/portal) and General Policies
(https://developer.riotgames.com/policies/general), current as of 2026.

**A 403 from Match-V5 with a key that worked yesterday is expected behavior**
for a personal/development key once it passes its 24-hour lifetime -- it is
not necessarily evidence of a bug in RuneSync's storage or request code.
Riot does not provide a way to programmatically distinguish "expired" from
"revoked" from "never valid" -- all three produce the same `401`/`403`, so
RuneSync's own status API (below) reports this uniformly as
`auth-rejected` rather than guessing at a specific cause.

## Riot's developer safety rules RuneSync follows

From Riot's General Policies (Developer Safety section):

- Don't share your Riot Games account or key with anyone outside your own
  organization/product.
- Don't use a production key across multiple, unrelated projects.
- Always use HTTPS when calling the API (RuneSync's `riot_api.py` only ever
  builds `https://` URLs).
- **Do not include your API key in your code, especially if you plan on
  distributing a binary.**

RuneSync's local key handling exists specifically to satisfy that last rule
for a packaged, redistributed `RuneSync.exe`:

- The key is never written into source, git history, the packaged
  PyInstaller resources (`RuneSync.spec` / `RuneSyncDebug.spec` `datas`),
  the Vault, SQLite, test fixtures, or any bridge/UI payload.
- It is encrypted at rest with Windows DPAPI (`secret_store.py`,
  `DpapiSecretBackend`), scoped to the current Windows user profile, at
  `%APPDATA%\RuneSync\riot_api_key.bin`. DPAPI keys are tied to the Windows
  user account, not the RuneSync install, so the file is useless if copied
  to another machine or user.
- Nothing in RuneSync ever logs, prints, returns over a bridge/IPC channel,
  or otherwise exposes the plaintext key. `RiotApiError` messages and
  `RiotSecretStore.__repr__`/`status()` are built to never include key
  material, only sanitized descriptions (see "Provider status API" below).

## Match-V5 is gated off by default

`RiotMatchV5Provider.fetch_match_timeline` refuses to run unless the
`RUNESYNC_ENABLE_PRIVATE_RIOT_MATCH_V5` environment variable is explicitly
set to a truthy value (`1`/`true`/`yes`/`on`). This is enforced inside the
provider itself -- it is not just a helper a caller could forget to check --
so a shipped build cannot accidentally start making Match-V5 calls with a
personal-use key, which Riot's policy does not allow for a distributed
product without going through its production key application process.

## History integration: an opt-in upgrade path, never a dependency

When enabled, `MatchHistoryService` (`match_history.py`) treats Match-V5 as
a best-effort upgrade layered strictly *after* LCU-derived history for a
game is already durable, both on fresh ingest (`ingest_game`) and during
backfill (`sync_recent`):

- **Platform routing is never guessed.** `platform_id_from_lcu_match`
  (`timeline_provider.py`) reads the authoritative `platformId` the local
  LCU match payload already carries (falling back to a participant
  identity's `currentPlatformId`/`platformId` if the top-level field is
  ever absent) and fails explicitly if neither is present or routable --
  it never assumes a default region.
- **Every failure mode is fully contained.** A disabled gate, missing/
  corrupt/rejected key, rate limit, upstream error, or a malformed/
  mismatched payload (wrong game ID, wrong participant count, empty/short
  frames, or Match-V5 participants that don't match what was already
  stored from LCU ingestion for that game) is caught locally and reduced
  to a bool plus sanitized status/backoff state. None of these can ever
  delay, block, or break the existing LCU-backed report or timeline.
- **Already-stored Match-V5 timelines are never refetched** (checked via
  `HistoryStore.has_timeline_payload`), and a failed attempt gets the same
  bounded, per-game backoff schedule as LCU timeline fetches (reusing
  `timeline_fetch_attempts`/`HistoryStore.timeline_fetch_due`), so a
  rejected key or a single bad match cannot retry-storm Riot or starve
  other matches of their own retry schedule. A rate-limit response during
  a backfill pass (`sync_recent`) stops that pass early rather than
  hammering the next game; each skipped game still keeps its own backoff
  schedule for a later pass.
- **Storage is immutable and content-addressed**, exactly like the
  existing LCU timeline path: Match-V5 payloads are saved via the same
  compressed, hash-deduplicated `timeline_payloads` table under the
  `match_v5` source, so re-saving an identical payload never creates a
  duplicate row.

See `tests/test_match_history.py` (gate disabled, missing/corrupt/rejected
key, routing, caching, backoff, fallback, malformed/mismatched payloads,
immutable storage) for the behavior this section describes.

## Provider status API

`riot_provider_status.py` exposes a small, closed set of sanitized states
via `ProviderStatus` and `get_riot_provider_status()`:

- `missing` -- no key has been saved yet
- `available` -- a key is saved, decrypts, and the last Match-V5 request (if
  any) succeeded
- `corrupt` -- the encrypted file exists but can't be decrypted/parsed
  (e.g. DPAPI unprotect failed, or it belongs to a different Windows user)
- `private-disabled` -- the research feature isn't opted into
- `auth-rejected` -- the last request got a `401`/`403` (expired/invalid/
  revoked key -- see the table above)
- `rate-limited` -- the last request got a `429`
- `upstream-unavailable` -- transient `5xx`/transport failure

This is a read-only status surface: it never returns, logs, or otherwise
exposes the key itself, and a rejected/expired key is **never cleared
automatically**. The encrypted file on disk is left exactly as-is so the
user can replace it through the normal `RiotSecretStore.set_key()` flow
(e.g. after regenerating a fresh personal key from the Developer Portal).

Bridge/UI wiring for this status API is a follow-up; today it is a
module-level Python API only (see `tests/test_riot_provider_status.py` for
usage examples).

## Practical guidance if Match-V5 returns 403

1. Personal/development keys expire every 24 hours. Regenerate one from
   https://developer.riotgames.com and save it through `RiotSecretStore`.
2. Confirm the platform routing is correct -- Match-V5 requires the
   *regional* routing value (`americas`/`europe`/`asia`/`sea`), not the
   platform shard; `riot_api.regional_route_for_platform` already handles
   this mapping, so a 403 is not a routing bug in this codebase.
3. If you intend to ship Match-V5 support in a distributed build, a
   personal key is not permitted for that -- you must register the product
   and obtain a production key from Riot first.
