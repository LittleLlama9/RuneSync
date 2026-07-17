"""Export and aggregate the DAEMON Score v2 synthetic review panel."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from corpus.synthetic_panel import (
    PANEL_ROUNDS,
    PanelValidationError,
    aggregate_judgments,
    export_panel_inputs,
    load_judgments,
    load_packets,
    save_aggregated_outputs,
    validate_panel_bundle,
)
from history_store import HistoryStore, default_history_path


def _parse_judgment_spec(spec: str) -> tuple[str, str, Path]:
    try:
        reviewer_id, round_id, path = spec.split("=", 2)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "judgment must be REVIEWER=ROUND=PATH"
        ) from exc
    if not reviewer_id or round_id not in PANEL_ROUNDS or not path:
        raise argparse.ArgumentTypeError(
            "judgment must use a non-empty reviewer and round a or b"
        )
    return reviewer_id, round_id, Path(path)


def _export(args) -> int:
    store = HistoryStore(args.history_db)
    result = export_panel_inputs(
        store, args.output_dir, evidence_source=args.evidence_source,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def _aggregate(args) -> int:
    packets_by_round = {
        round_id: load_packets(args.input_dir / f"round-{round_id}.jsonl")
        for round_id in PANEL_ROUNDS
    }
    private_document = json.loads(
        (args.input_dir / "private-map.json").read_text(encoding="utf-8")
    )
    private_maps = validate_panel_bundle(packets_by_round, private_document)
    judgment_rows = {}
    for reviewer_id, round_id, path in args.judgment:
        judgment_rows[(reviewer_id, round_id)] = load_judgments(
            path, packets_by_round[round_id],
        )
    labels, token_maps, report = aggregate_judgments(
        packets_by_round=packets_by_round,
        private_maps=private_maps,
        judgments=judgment_rows,
        generated_at=private_document["generated_at"],
        min_reviewers=args.min_reviewers,
        min_agreement=args.min_agreement,
        min_confidence=args.min_confidence,
        max_tier_gap=args.max_tier_gap,
    )
    save_aggregated_outputs(
        labels=labels, token_maps=token_maps, report=report,
        labels_path=args.labels_output,
        token_map_path=args.token_map_output,
        report_path=args.report_output,
    )
    print(json.dumps(report, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser(
        "export", help="Export two shuffled, outcome-blind judge rounds",
    )
    export_parser.add_argument(
        "--history-db", type=Path, default=default_history_path(),
    )
    export_parser.add_argument("--output-dir", type=Path, required=True)
    export_parser.add_argument(
        "--evidence-source", default="lcu_timeline",
    )
    export_parser.set_defaults(handler=_export)

    aggregate_parser = subparsers.add_parser(
        "aggregate", help="Validate judge JSONL and emit consensus labels",
    )
    aggregate_parser.add_argument("--input-dir", type=Path, required=True)
    aggregate_parser.add_argument(
        "--judgment", action="append", type=_parse_judgment_spec,
        required=True, help="REVIEWER=ROUND=PATH; repeat per reviewer/round",
    )
    aggregate_parser.add_argument("--labels-output", type=Path, required=True)
    aggregate_parser.add_argument("--token-map-output", type=Path, required=True)
    aggregate_parser.add_argument("--report-output", type=Path, required=True)
    aggregate_parser.add_argument("--min-reviewers", type=int, default=3)
    aggregate_parser.add_argument("--min-agreement", type=float, default=1.0)
    aggregate_parser.add_argument("--min-confidence", type=float, default=0.65)
    aggregate_parser.add_argument(
        "--max-tier-gap", type=int, default=3,
        help="Keep only comparisons separated by at most this many judge tiers",
    )
    aggregate_parser.set_defaults(handler=_aggregate)

    args = parser.parse_args()
    try:
        return args.handler(args)
    except (
            OSError, KeyError, TypeError, ValueError,
            PanelValidationError) as exc:
        print(f"FAILED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
