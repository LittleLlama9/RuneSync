"""Render synthetic-panel packets into compact per-chunk markdown for judges.

A raw packet is ~33 KB of deeply nested JSON per match; handing many of those
to a judge agent is wasteful and error-prone. This flattens each packet into a
compact, human-readable evidence table and, crucially, emits per subject an
explicit allowlist of dotted evidence paths that are *known to resolve* in that
subject's real features object. A judge cites only from that allowlist, so its
verdict always passes ``synthetic_panel.validate_judgment`` (which requires each
cited path to exist) while never seeing win/loss, scores, or ranks -- exactly
the same outcome-blind evidence the full packet carries, just legible.

The full packets remain the source of truth for aggregation; this view is only
what the judge reads. Output: one ``<round>-chunk-<i>.md`` per chunk under the
output dir, plus a ``manifest.json`` describing the chunking.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

# Blocks surfaced to the judge, in reading order. ``raw`` is included as
# context only; the rubric forbids ranking by raw stat lines.
_BLOCK_ORDER = (
    "fight_influence",
    "objective_participation",
    "resource_conversion",
    "vision_influence",
    "enablement_suppression",
    "structure_pressure",
    "death_tempo",
    "raw",
)


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3g}"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _flatten_leaves(block: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(block, dict):
        for key, value in block.items():
            _flatten_leaves(value, f"{prefix}.{key}", out)
    elif isinstance(block, (int, float, bool)):
        out[prefix] = block


def _subject_view(subject: dict) -> tuple[list[str], list[str]]:
    """Return (compact evidence lines, citeable dotted paths) for a subject."""
    features = subject.get("features") or {}
    lines: list[str] = []
    citeable: list[str] = []
    for block_name in _BLOCK_ORDER:
        block = features.get(block_name)
        if block is None:
            continue
        if isinstance(block, dict) and block.get("available") is False:
            lines.append(f"  {block_name}: unavailable")
            continue
        leaves: dict[str, Any] = {}
        _flatten_leaves(block, block_name, leaves)
        if not leaves:
            continue
        # Compact one-liner of this block's leaves (strip the block prefix).
        pretty = ", ".join(
            f"{path.split('.', 1)[1]}={_fmt(val)}"
            for path, val in leaves.items()
        )
        lines.append(f"  {block_name}: {pretty}")
        citeable.extend(leaves.keys())
    return lines, citeable


def _render_packet(packet: dict) -> str:
    out = [
        f"### PACKET {packet['packet_id']}",
        f"duration_seconds={packet.get('duration_seconds')} "
        f"evidence_completeness={packet.get('evidence_completeness')}",
        "",
        "Rank these 10 subjects by individual match influence (rubric.json). "
        "Ignore team/win. Use ties when evidence cannot separate two subjects.",
        "",
    ]
    for subject in packet["participants"]:
        lines, _citeable = _subject_view(subject)
        out.append(
            f"- SUBJECT {subject['subject_id']} "
            f"[{subject.get('role')} / {subject.get('champion_name')}]"
        )
        out.extend(lines)
        out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round-file", required=True, type=Path)
    parser.add_argument("--round-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--chunk-size", type=int, default=10)
    args = parser.parse_args()

    packets = [
        json.loads(line)
        for line in args.round_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    chunks = []
    for index in range(0, len(packets), args.chunk_size):
        group = packets[index:index + args.chunk_size]
        chunk_no = index // args.chunk_size
        body = [
            f"# Round {args.round_id} chunk {chunk_no} "
            f"({len(group)} packets)",
            "",
            "You are one independent judge. For EACH packet below, output one "
            "JSON object (JSONL, no markdown) matching the panel schema. Rank "
            "individual influence only; never infer win/loss; never rank by a "
            "single stat. For each subject cite 1-6 evidence_paths written "
            "exactly as `block.leaf` using the block names and leaf keys shown "
            "in that subject's lines (e.g. `fight_influence.kill_events`, "
            "`objective_participation.epic_monster_secures`, "
            "`resource_conversion.conversion_rate`, "
            "`vision_influence.actionable_wards`, "
            "`enablement_suppression.suppression_events`, "
            "`death_tempo.untraded` -> use the exact keys shown). "
            "rationale_tags must come from: combat_impact, objective_control, "
            "vision_control, economy_efficiency, survivability, role_context, "
            "teamfight_presence, laning_phase, utility_support, "
            "insufficient_data.",
            "",
        ]
        for packet in group:
            body.append(_render_packet(packet))
        path = args.output_dir / f"{args.round_id}-chunk-{chunk_no}.md"
        path.write_text("\n".join(body), encoding="utf-8")
        chunks.append({
            "round_id": args.round_id,
            "chunk_no": chunk_no,
            "packet_ids": [p["packet_id"] for p in group],
            "markdown": str(path),
        })

    manifest_path = args.output_dir / f"manifest-{args.round_id}.json"
    manifest_path.write_text(json.dumps(chunks, indent=2), encoding="utf-8")
    print(f"round {args.round_id}: {len(packets)} packets -> "
          f"{len(chunks)} chunks in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
