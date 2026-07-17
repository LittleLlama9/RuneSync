"""Build a read-only Score v2 shadow report from retained local evidence.

The command never saves a score run or changes the active score pointer.
``--backfill-features`` explicitly permits canonical feature-set persistence.
Development artifacts require ``--allow-development-artifacts`` and neutral
``insufficient_data`` artifacts are always rejected.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from history_store import HistoryStore
from performance_score import ScoreRoutingError, load_score_v2_artifacts
from score_v2.shadow import ShadowReportError, build_shadow_report


ROOT = Path(__file__).resolve().parent.parent.parent


def _load_cases(path: Path) -> list[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("cases") or ())


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-db", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--artifacts-dir", type=Path)
    parser.add_argument("--allow-development-artifacts", action="store_true")
    parser.add_argument("--backfill-features", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--game-id", type=int, action="append", dest="game_ids")
    parser.add_argument(
        "--adversarial-cases",
        type=Path,
        default=ROOT / "corpus" / "data" / "adversarial_cases.json",
    )
    args = parser.parse_args()

    try:
        artifacts = (
            load_score_v2_artifacts(
                args.artifacts_dir,
                require_production_ready=not args.allow_development_artifacts,
            )
            if args.artifacts_dir else {}
        )
        if args.artifacts_dir and not artifacts:
            raise ShadowReportError(
                f"No valid exact-tier artifacts were found in "
                f"{args.artifacts_dir}."
            )
        report = build_shadow_report(
            HistoryStore(args.history_db),
            artifacts,
            game_ids=args.game_ids,
            limit=args.limit,
            backfill_features=args.backfill_features,
            allow_development_artifacts=args.allow_development_artifacts,
            adversarial_cases=_load_cases(args.adversarial_cases),
        )
    except (
            OSError, json.JSONDecodeError, ShadowReportError,
            ScoreRoutingError, TypeError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    try:
        _write_json(args.output, report)
    except OSError as exc:
        print(f"FAILED: could not write {args.output}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    if report["summary"]["status_counts"].get("error"):
        print(
            "FAILED: one or more games could not be analyzed; inspect the "
            "written report.", file=sys.stderr,
        )
        return 2
    failed_cases = [
        row for row in report["adversarial_cases"]
        if row.get("passed") is False
    ]
    if failed_cases:
        print(
            f"FAILED: {len(failed_cases)} verified adversarial case(s) failed.",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
