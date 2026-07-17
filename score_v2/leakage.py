"""Recursive outcome-leakage guard shared by training and runtime.

DAEMON Score v2 must never let a game's win/loss (or any other direct
result signal -- nexus destruction, "game end" bookkeeping, surrender,
remake) leak into a per-participant *feature* used as model input. Outcome
information may only ever exist as an explicitly separate, auxiliary
offline state-value label (see `score_v2.training.dataset.StateValueLabel`)
-- never as an individual score input, and never accepted at runtime.

`score_features.py` already strips `win`/`local_win` from participant rows
before any feature is computed (`_OUTCOME_KEYS`), and `score_v2.feature_spec`
only ever reads a fixed, hand-reviewed allowlist of dotted paths out of a
participant's feature block. This module is the defense-in-depth backstop
for both of those, in the same spirit as `corpus/_privacy.py`'s
`scan_for_forbidden`: a corrupted, hand-edited, or future-extended payload
should not be trusted just because today's extractor code is safe.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

# Whole-WORD denylist, checked against a key after it is split into
# snake_case/kebab-case/camelCase tokens -- not a raw substring match.
# ("win" as a raw substring would also match "lead_windows" and
# "resource_conversion.converted_lead_windows" from `score_features.py`'s
# own resource-conversion family, which are legitimate time-window fields
# with nothing to do with game outcome; splitting on word boundaries first
# avoids that false positive while still catching "win", "local_win",
# "teamWin", "didWin", etc.)
OUTCOME_WORDS = frozenset({
    "win", "wins", "won", "winner", "winners",
    "lose", "loses", "lost", "loss", "losses",
    "result", "results",
    "nexus",
    "victory", "victories",
    "defeat", "defeats", "defeated",
    "surrender", "surrendered", "surrenders",
    "remake", "remakes",
    "outcome", "outcomes",
})

# Adjacent-token phrases (order-independent) that are only meaningful
# together -- e.g. a lone "game" or "end" key is harmless (`game_creation`,
# `end_ms`), but the pair means "game end"/"end of game" bookkeeping.
OUTCOME_ADJACENT_PHRASES = (
    frozenset({"game", "end"}),
    frozenset({"game", "over"}),
)


class OutcomeLeakageError(Exception):
    """Raised when a payload carries a direct game-outcome signal."""


def _tokenize_key(key: Any) -> list[str]:
    """Split a key into lowercase word tokens across snake/kebab/camelCase."""
    text = str(key)
    text = re.sub(r"[_\-\s]+", " ", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    return [token.lower() for token in text.split() if token]


def _key_problems(key_str: str) -> list[str]:
    tokens = _tokenize_key(key_str)
    problems = []
    for token in tokens:
        if token in OUTCOME_WORDS:
            problems.append(
                f"forbidden outcome-shaped key (word '{token}' in '{key_str}')"
            )
    for left, right in zip(tokens, tokens[1:]):
        pair = frozenset({left, right})
        if pair in OUTCOME_ADJACENT_PHRASES:
            problems.append(
                f"forbidden outcome-shaped key (phrase '{left} {right}' in "
                f"'{key_str}')"
            )
    return problems


def scan_for_outcome_leakage(obj: Any, *, _path: str = "$") -> list[str]:
    """Recursively find outcome-shaped keys anywhere inside `obj`.

    Returns a list of human-readable problem descriptions (empty if
    clean). Never raises -- callers decide whether findings are fatal via
    `assert_no_outcome_leakage`. Mirrors `corpus/_privacy.scan_for_forbidden`'s
    shape so both packages are auditable the same way.
    """
    problems: list[str] = []
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            key_str = str(key)
            for problem in _key_problems(key_str):
                problems.append(f"{_path}.{key_str}: {problem}")
            problems.extend(
                scan_for_outcome_leakage(value, _path=f"{_path}.{key_str}")
            )
    elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
        for index, value in enumerate(obj):
            problems.extend(
                scan_for_outcome_leakage(value, _path=f"{_path}[{index}]")
            )
    return problems


def assert_no_outcome_leakage(obj: Any, *, context: str = "") -> None:
    """Raise `OutcomeLeakageError` if `obj` contains an outcome-shaped key."""
    problems = scan_for_outcome_leakage(obj)
    if problems:
        prefix = f"{context}: " if context else ""
        raise OutcomeLeakageError(prefix + "; ".join(problems))


def validate_feature_payload(participant_features: Mapping) -> None:
    """Guard the entry point every feature extractor must call first.

    `participant_features` is one participant's block from
    `score_features.compute_feature_set` (or an equivalent externally
    loaded/replayed payload). Raises before any `score_v2.feature_spec`
    path is evaluated against it.
    """
    assert_no_outcome_leakage(
        participant_features, context="participant feature payload",
    )
