"""Select a stratified sample of corpus games for synthetic-panel grading.

Panel grading (independent LLM judges) is the expensive, rate-limited step, so
a large ingested corpus is graded on a representative *subset* rather than in
full. This picks a balanced sample across queue domain (ranked solo / normal
draft / ranked flex), game-length bucket, and patch-major, then writes a plain
whitespace-separated game-ID file consumable by
``run_synthetic_panel.py export --game-ids-file``.

Balancing matters because the trained model must not overfit to, say, only long
ranked-solo games; spreading the (limited) labels across situations gives the
ranking objective varied contexts. Outcome balance is automatic: every game
contributes exactly five winners and five losers.

Usage:
    py scripts/score_v2/sample_corpus.py \
        --corpus-db C:\\Users\\Matth\\RuneSyncData\\AngryBacteria\\corpus\\history.db \
        --count 100 --seed panel-2026-07 --output sample-game-ids.txt
"""

from __future__ import annotations

import argparse
import collections
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from corpus.angrybacteria import QUEUE_DOMAIN  # noqa: E402
from history_store import HistoryStore  # noqa: E402


def _duration_bucket(seconds: int) -> str:
    if seconds < 20 * 60:
        return "short"
    if seconds < 30 * 60:
        return "medium"
    return "long"


def _patch_major(patch: str) -> str:
    parts = str(patch or "").split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return "unknown"


def _stratum(match: dict) -> tuple[str, str, str]:
    domain = QUEUE_DOMAIN.get(match.get("queue_id"), "unknown")
    return (domain, _duration_bucket(match.get("duration", 0)),
            _patch_major(match.get("patch")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-db", required=True, type=Path)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--seed", default="panel-2026-07")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    store = HistoryStore(args.corpus_db)
    game_ids = sorted(store.known_game_ids())
    if not game_ids:
        print("corpus DB has no games", file=sys.stderr)
        return 1

    strata: dict[tuple, list[int]] = collections.defaultdict(list)
    meta: dict[int, dict] = {}
    for gid in game_ids:
        match = store.get_match(gid)
        meta[gid] = match
        strata[_stratum(match)].append(gid)

    rng = random.Random(args.seed)
    for bucket in strata.values():
        rng.shuffle(bucket)

    # Round-robin draw across strata so the sample mirrors corpus diversity
    # without letting one dominant stratum crowd out rare ones.
    order = sorted(strata, key=lambda k: (-len(strata[k]), k))
    selected: list[int] = []
    exhausted = set()
    while len(selected) < min(args.count, len(game_ids)):
        progressed = False
        for key in order:
            if key in exhausted:
                continue
            bucket = strata[key]
            if not bucket:
                exhausted.add(key)
                continue
            selected.append(bucket.pop())
            progressed = True
            if len(selected) >= args.count:
                break
        if not progressed:
            break

    selected.sort()
    args.output.write_text("\n".join(str(g) for g in selected) + "\n",
                           encoding="utf-8")

    dist_domain = collections.Counter(
        QUEUE_DOMAIN.get(meta[g].get("queue_id"), "unknown") for g in selected)
    dist_len = collections.Counter(
        _duration_bucket(meta[g].get("duration", 0)) for g in selected)
    dist_patch = collections.Counter(
        _patch_major(meta[g].get("patch")) for g in selected)
    print(f"corpus games: {len(game_ids)}  strata: {len(strata)}")
    print(f"selected: {len(selected)} -> {args.output}")
    print(f"  domain: {dict(dist_domain.most_common())}")
    print(f"  length: {dict(dist_len.most_common())}")
    print(f"  patch:  {dict(dist_patch.most_common())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
