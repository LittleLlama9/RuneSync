"""Ingest the AngryBacteria dump into a private DAEMON Score v2 corpus DB.

Two streaming passes join the independently ordered match and timeline files on
``matchId``; only games present in *both* (with a full Rift-5v5 structure) are
committed, each as a v1-scored match plus a stored Match-V5 timeline payload
plus an extracted Score v2 feature set. Identities are reduced to a salted
group key (see ``corpus/angrybacteria.py``); the salt is generated once and
kept locally so re-runs group the same player consistently without ever
persisting a raw Riot PUUID.

Usage:
    py scripts/score_v2/ingest_angrybacteria.py \
        --match-file  C:\\Users\\Matth\\RuneSyncData\\AngryBacteria\\match_v5.json \
        --timeline-file C:\\Users\\Matth\\RuneSyncData\\AngryBacteria\\timeline_v5.json \
        --corpus-db  C:\\Users\\Matth\\RuneSyncData\\AngryBacteria\\corpus\\history.db \
        --limit 1500
"""

from __future__ import annotations

import argparse
import collections
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from corpus.angrybacteria import (  # noqa: E402
    QUEUE_DOMAIN,
    SkipMatch,
    TIMELINE_SOURCE,
    assert_no_identity_leak,
    build_report,
    build_timeline_payload,
    stream_documents,
)
from history_store import HistoryStore  # noqa: E402
from score_features import extract_game_features  # noqa: E402


def _load_or_create_salt(corpus_dir: Path) -> bytes:
    salt_path = corpus_dir / "identity_salt.bin"
    if salt_path.exists():
        return salt_path.read_bytes()
    corpus_dir.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    salt_path.write_bytes(salt)
    return salt


def _collect_candidates(match_file: str, salt: bytes,
                        max_candidates: int) -> tuple[dict, collections.Counter]:
    """Pass 1: eligible ``matchId -> report`` from the match summary file."""
    candidates: dict[str, dict] = {}
    skips: collections.Counter = collections.Counter()
    seen = 0
    for document in stream_documents(match_file):
        seen += 1
        match_id = (document.get("metadata") or {}).get("matchId")
        info = document.get("info") or {}
        try:
            report = build_report(info, salt)
        except SkipMatch as skip:
            skips[skip.reason.split(":", 1)[0]] += 1
            continue
        except Exception as exc:  # noqa: BLE001 - keep ingest resilient, tally it
            skips[f"error:{type(exc).__name__}"] += 1
            continue
        if not match_id:
            skips["missing_match_id"] += 1
            continue
        report["match"]["queue_domain"] = QUEUE_DOMAIN.get(
            report["match"]["queue_id"], "unknown")
        candidates[match_id] = report
        if seen % 2000 == 0:
            print(f"  [match pass] scanned {seen}, eligible {len(candidates)}",
                  flush=True)
        if len(candidates) >= max_candidates:
            print(f"  [match pass] hit max-candidates {max_candidates}, "
                  f"stopping scan at {seen} docs", flush=True)
            break
    return candidates, skips


def _commit(store: HistoryStore, report: dict, timeline_info: dict) -> str:
    """Persist one joined game and extract its features. Returns the domain."""
    assert_no_identity_leak(report)
    game_id = report["match"]["game_id"]
    payload = build_timeline_payload(timeline_info)
    frames = payload["timeline"]["frames"]
    if len(frames) < 2:
        raise SkipMatch(f"timeline_too_short:{len(frames)}")
    store.save_report(report)
    store.save_timeline_payload(game_id, TIMELINE_SOURCE, payload,
                               completeness=1.0)
    extract_game_features(store, game_id, evidence_source=TIMELINE_SOURCE)
    return report["match"].get("queue_domain", "unknown")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match-file", required=True)
    parser.add_argument("--timeline-file", required=True)
    parser.add_argument("--corpus-db", required=True)
    parser.add_argument("--limit", type=int, default=1500,
                        help="max games to commit (with both match and timeline)")
    parser.add_argument("--max-candidates", type=int, default=20000,
                        help="cap on eligible match records held in memory")
    args = parser.parse_args()

    corpus_db = Path(args.corpus_db)
    corpus_dir = corpus_db.parent
    salt = _load_or_create_salt(corpus_dir)
    store = HistoryStore(corpus_db)

    started = time.time()
    print("Pass 1/2: scanning match summaries for eligible Rift-5v5 games...",
          flush=True)
    candidates, skips = _collect_candidates(
        args.match_file, salt, args.max_candidates)
    print(f"  eligible candidates: {len(candidates)}", flush=True)
    print(f"  skip reasons: {dict(skips.most_common())}", flush=True)

    already = set(store.known_game_ids())
    print("Pass 2/2: joining timelines and committing...", flush=True)
    committed = 0
    domain_counts: collections.Counter = collections.Counter()
    join_skips: collections.Counter = collections.Counter()
    scanned_tl = 0
    for document in stream_documents(args.timeline_file):
        scanned_tl += 1
        match_id = (document.get("metadata") or {}).get("matchId")
        if match_id not in candidates:
            continue
        report = candidates[match_id]
        if report["match"]["game_id"] in already:
            join_skips["already_ingested"] += 1
            continue
        info = document.get("info") or {}
        try:
            domain = _commit(store, report, info)
        except SkipMatch as skip:
            join_skips[skip.reason.split(":", 1)[0]] += 1
            continue
        except Exception as exc:  # noqa: BLE001
            join_skips[f"error:{type(exc).__name__}"] += 1
            continue
        domain_counts[domain] += 1
        committed += 1
        if committed % 100 == 0:
            rate = committed / max(time.time() - started, 1e-9)
            print(f"  committed {committed} (tl scanned {scanned_tl}) "
                  f"{rate:.1f}/s", flush=True)
        if committed >= args.limit:
            print(f"  reached limit {args.limit}", flush=True)
            break

    elapsed = time.time() - started
    print("Done.", flush=True)
    print(f"  committed: {committed}", flush=True)
    print(f"  domain mix: {dict(domain_counts.most_common())}", flush=True)
    print(f"  join skips: {dict(join_skips.most_common())}", flush=True)
    print(f"  elapsed: {elapsed:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
