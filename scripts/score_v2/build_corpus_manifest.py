"""Build a DAEMON Score v2 corpus manifest from a HistoryStore database.

There was previously no CLI to turn an ingested ``history.db`` into a
``corpus/manifest.py`` manifest -- the personal-beta manifest was built inline.
This script fills that gap for the AngryBacteria (and any future) corpus: it
reads (never writes) a store, builds one sanitized :class:`ManifestEntry` per
requested game for a chosen evidence source via
:func:`corpus.build_from_history.build_entry_from_history`, and saves a
validated manifest JSON. It fabricates nothing: a game without the requested
evidence source raises rather than being silently dropped, unless
``--skip-missing`` is given (then the skip is counted and reported).

Game selection:
- ``--game-ids-file``: newline-separated game ids (blank lines ignored), or
- ``--all``: every game the store reports via ``known_game_ids``.

Usage:
  py scripts/score_v2/build_corpus_manifest.py \
     --history-db path/to/history.db --salt path/to/identity_salt.bin \
     --source match_v5 --game-ids-file sample-30.txt --output manifest.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from corpus.build_from_history import (  # noqa: E402
    HistoryEvidenceUnavailableError,
    build_entry_from_history,
)
from corpus.manifest import CorpusManifest  # noqa: E402
from history_store import HistoryStore  # noqa: E402


def _load_game_ids(path: Path) -> list[int]:
    ids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            ids.append(int(line))
    return ids


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-db", required=True)
    parser.add_argument("--salt", required=True,
                        help="Path to the 32-byte identity salt file.")
    parser.add_argument("--source", default="match_v5",
                        choices=["match_v5", "lcu_timeline", "aggregate",
                                 "live_client"])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--game-ids-file")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--output", required=True)
    parser.add_argument("--skip-missing", action="store_true",
                        help="Count and report games lacking the source "
                             "instead of failing.")
    args = parser.parse_args(argv)

    salt = Path(args.salt).read_bytes()
    store = HistoryStore(Path(args.history_db))
    if args.all:
        game_ids = sorted(store.known_game_ids())
    else:
        game_ids = _load_game_ids(Path(args.game_ids_file))

    manifest = CorpusManifest()
    built = 0
    skipped = []
    for game_id in game_ids:
        try:
            entry = build_entry_from_history(
                store, game_id, args.source, identity_salt=salt,
            )
        except HistoryEvidenceUnavailableError as exc:
            if args.skip_missing:
                skipped.append((game_id, str(exc)))
                continue
            raise
        manifest.add_entry(entry)
        built += 1

    manifest.save(Path(args.output))

    print({
        "requested": len(game_ids),
        "built": built,
        "skipped": len(skipped),
        "source": args.source,
        "output": args.output,
        "stats": manifest.stats(),
    })
    for game_id, reason in skipped[:5]:
        print(f"  skipped {game_id}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
