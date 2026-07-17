"""Deterministic corpus manifest format for DAEMON Score v2.

A manifest entry describes one piece of evidence for one local match: which
of the four evidence tiers it came from (Match-V5 > LCU post-game timeline >
Live Client capture > aggregate LCU fallback -- see the vault decision
"Promote LCU post-game timelines into DAEMON Score v2 evidence hierarchy"),
what capabilities that tier actually has, how complete this particular
instance is, and what privacy/consent rules govern it. Entries never carry a
raw PUUID, summoner name, Riot API key, or the raw timeline/Match-V5 payload
itself -- only sanitized metadata plus a content hash that lets a caller
detect when the *real* underlying payload (stored separately by
``history_store.py``) changes.

Fields are deliberately honest about gaps rather than inventing values: the
current ``history_store.py`` schema does not capture region/platform or
rank/tier at all, so entries built from it record ``None`` with an explicit
reason rather than a guessed value. See ``build_from_history.py`` for the
concrete builder that reads (never writes) ``HistoryStore``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from corpus._privacy import scan_for_forbidden

MANIFEST_SCHEMA_VERSION = 1

EVIDENCE_SOURCES = ("match_v5", "lcu_timeline", "live_client", "aggregate")

PRIVACY_CLASSIFICATIONS = ("local_hashed_real", "local_hashed_synthetic")

CONSENT_STATUSES = (
    # LCU/Live Client Data API evidence comes from the user's own local game
    # client, not a Riot public endpoint -- no Match-V5 developer policy
    # applies to it.
    "personal_local_client_data",
    # Match-V5 evidence collected under a personal/development key, gated by
    # RUNESYNC_ENABLE_PRIVATE_RIOT_MATCH_V5 (see docs/RIOT_API_KEY_POLICY.md).
    # Personal keys may not be redistributed or used in a shipped product.
    "match_v5_personal_key_research_only",
    # Bulk/authorized Match-V5 acquisition is currently blocked pending a
    # Riot production key approval (see the vault decision "Gate final Score
    # v2 validation on Match-V5 authorization, not local feature
    # development"). This status is used for entries whose provenance
    # anticipates that gate rather than pretending it is already open.
    "match_v5_authorization_blocked",
    # Deliberately constructed adversarial examples contain no real player
    # data and therefore do not require player consent.
    "synthetic_no_consent_required",
    "unknown",
)

CAPABILITY_FLAG_NAMES = (
    "has_all_player_parity",
    "has_local_player_detail",
    "has_position_timeline",
    "has_minute_frames",
    "has_subminute_resolution",
    "has_champion_events",
    "has_building_events",
    "has_item_events",
    "has_ward_events",
    "has_victim_damage_detail",
    "has_bounty_detail",
)

# Defaults per evidence tier, grounded in the vault decision that split the
# four-tier hierarchy: Match-V5 has the richest events but is minute-frame
# resolution (not sub-minute); the LCU post-game timeline has minute frames
# and position/gold/XP/CS plus champion/objective/building events but omits
# victim damage records, item/ward events, and bounty detail; Live Client
# capture is sub-minute but local-player-centric (opponents are not at
# parity); the aggregate fallback is final-stats-only with no timeline at
# all.
DEFAULT_CAPABILITY_FLAGS: dict[str, dict[str, bool]] = {
    "match_v5": {
        "has_all_player_parity": True,
        "has_local_player_detail": True,
        "has_position_timeline": True,
        "has_minute_frames": True,
        "has_subminute_resolution": False,
        "has_champion_events": True,
        "has_building_events": True,
        "has_item_events": True,
        "has_ward_events": True,
        "has_victim_damage_detail": True,
        "has_bounty_detail": True,
    },
    "lcu_timeline": {
        "has_all_player_parity": True,
        "has_local_player_detail": True,
        "has_position_timeline": True,
        "has_minute_frames": True,
        "has_subminute_resolution": False,
        "has_champion_events": True,
        "has_building_events": True,
        "has_item_events": False,
        "has_ward_events": False,
        "has_victim_damage_detail": False,
        "has_bounty_detail": False,
    },
    "live_client": {
        "has_all_player_parity": False,
        "has_local_player_detail": True,
        "has_position_timeline": False,
        "has_minute_frames": False,
        "has_subminute_resolution": True,
        "has_champion_events": False,
        "has_building_events": False,
        "has_item_events": False,
        "has_ward_events": False,
        "has_victim_damage_detail": False,
        "has_bounty_detail": False,
    },
    "aggregate": {
        "has_all_player_parity": True,
        "has_local_player_detail": True,
        "has_position_timeline": False,
        "has_minute_frames": False,
        "has_subminute_resolution": False,
        "has_champion_events": False,
        "has_building_events": False,
        "has_item_events": False,
        "has_ward_events": False,
        "has_victim_damage_detail": False,
        "has_bounty_detail": False,
    },
}


class ManifestValidationError(Exception):
    """Raised when a manifest entry violates the corpus policy."""


def canonical_json(obj) -> str:
    """Deterministic JSON encoding used for hashing and on-disk storage."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def default_corpus_dir() -> Path:
    """Local-only directory for corpus/review state, never inside the repo.

    Mirrors ``history_store.default_history_path`` so this package's runtime
    state (manifest, identity salt, review labels) lives next to
    ``history.db`` under the user's own profile, not in version control.
    """
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    directory = base / "RuneSync" / "corpus"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        directory = Path(tempfile.gettempdir()) / "RuneSync" / "corpus"
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def load_or_create_identity_salt(path: Optional[Path] = None) -> bytes:
    """Return the local salt used to pseudonymize PUUIDs for grouping.

    The salt never leaves this machine and is never derived from any Riot
    identifier. Rotating or deleting the salt file is the documented
    "forget" mechanism: it severs the link between previously hashed local
    player ids and any future ones, without touching ``history_store.py``'s
    own data (see docs/CORPUS_AND_REVIEW.md, "Retention and deletion").
    """
    salt_path = Path(path) if path else (default_corpus_dir() / "identity_salt.bin")
    salt_path.parent.mkdir(parents=True, exist_ok=True)
    if salt_path.exists():
        raw = salt_path.read_bytes()
        if raw:
            return raw
    raw = os.urandom(32)
    salt_path.write_bytes(raw)
    return raw


def hash_identifier(raw_value: str, salt: bytes) -> str:
    """Pseudonymize a raw Riot identifier (PUUID) into a stable local id.

    Deterministic given the same salt (so the same player groups correctly
    across entries), but not reversible without that locally-held salt, and
    never stores or logs ``raw_value`` itself.
    """
    digest = hashlib.sha256(salt + raw_value.encode("utf-8")).hexdigest()
    return f"p_{digest[:24]}"


@dataclass(frozen=True)
class Provenance:
    capture_method: str
    collection_tool: str = "runesync-corpus-tooling"
    collection_tool_version: str = "1"
    source_schema_version: str = "1"
    captured_at: str = ""
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "capture_method": self.capture_method,
            "collection_tool": self.collection_tool,
            "collection_tool_version": self.collection_tool_version,
            "source_schema_version": self.source_schema_version,
            "captured_at": self.captured_at,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "Provenance":
        return cls(
            capture_method=data["capture_method"],
            collection_tool=data.get("collection_tool", "runesync-corpus-tooling"),
            collection_tool_version=data.get("collection_tool_version", "1"),
            source_schema_version=data.get("source_schema_version", "1"),
            captured_at=data.get("captured_at", ""),
            notes=tuple(data.get("notes", ())),
        )


@dataclass(frozen=True)
class GameMetadata:
    patch: Optional[str] = None
    queue_id: Optional[int] = None
    map_id: Optional[int] = None
    duration_seconds: Optional[int] = None
    game_creation_date: Optional[str] = None
    region: Optional[str] = None
    region_unknown_reason: Optional[str] = None
    rank_tier: Optional[str] = None
    rank_unknown_reason: Optional[str] = None

    def __post_init__(self):
        if self.region is None and not self.region_unknown_reason:
            object.__setattr__(
                self, "region_unknown_reason", "region_not_supplied"
            )
        if self.rank_tier is None and not self.rank_unknown_reason:
            object.__setattr__(
                self, "rank_unknown_reason", "rank_not_supplied"
            )

    def to_dict(self) -> dict:
        return {
            "patch": self.patch,
            "queue_id": self.queue_id,
            "map_id": self.map_id,
            "duration_seconds": self.duration_seconds,
            "game_creation_date": self.game_creation_date,
            "region": self.region,
            "region_unknown_reason": self.region_unknown_reason,
            "rank_tier": self.rank_tier,
            "rank_unknown_reason": self.rank_unknown_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "GameMetadata":
        return cls(**{key: data.get(key) for key in (
            "patch", "queue_id", "map_id", "duration_seconds",
            "game_creation_date", "region", "region_unknown_reason",
            "rank_tier", "rank_unknown_reason",
        )})


@dataclass(frozen=True)
class LeakageKeys:
    match_group_key: str
    player_group_keys: tuple[str, ...]
    champion: Optional[str] = None
    role: Optional[str] = None
    region: Optional[str] = None
    rank_tier: Optional[str] = None
    patch: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "match_group_key": self.match_group_key,
            "player_group_keys": list(self.player_group_keys),
            "champion": self.champion,
            "role": self.role,
            "region": self.region,
            "rank_tier": self.rank_tier,
            "patch": self.patch,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "LeakageKeys":
        return cls(
            match_group_key=data["match_group_key"],
            player_group_keys=tuple(data.get("player_group_keys", ())),
            champion=data.get("champion"),
            role=data.get("role"),
            region=data.get("region"),
            rank_tier=data.get("rank_tier"),
            patch=data.get("patch"),
        )


@dataclass(frozen=True)
class ManifestEntry:
    entry_id: str
    game_id: int
    source: str
    manifest_schema_version: int
    provenance: Provenance
    content_hash: str
    capability_flags: Mapping[str, bool]
    completeness: float
    privacy_classification: str
    consent_status: str
    game_metadata: GameMetadata
    leakage: LeakageKeys
    created_at: str

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "game_id": self.game_id,
            "source": self.source,
            "manifest_schema_version": self.manifest_schema_version,
            "provenance": self.provenance.to_dict(),
            "content_hash": self.content_hash,
            "capability_flags": dict(self.capability_flags),
            "completeness": self.completeness,
            "privacy_classification": self.privacy_classification,
            "consent_status": self.consent_status,
            "game_metadata": self.game_metadata.to_dict(),
            "leakage": self.leakage.to_dict(),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "ManifestEntry":
        return cls(
            entry_id=data["entry_id"],
            game_id=int(data["game_id"]),
            source=data["source"],
            manifest_schema_version=int(data["manifest_schema_version"]),
            provenance=Provenance.from_dict(data["provenance"]),
            content_hash=data["content_hash"],
            capability_flags=dict(data["capability_flags"]),
            completeness=float(data["completeness"]),
            privacy_classification=data["privacy_classification"],
            consent_status=data["consent_status"],
            game_metadata=GameMetadata.from_dict(data["game_metadata"]),
            leakage=LeakageKeys.from_dict(data["leakage"]),
            created_at=data["created_at"],
        )


def _fact_hash(
        game_id: int, source: str, capability_flags: Mapping[str, bool],
        completeness: float, game_metadata: GameMetadata,
        leakage: LeakageKeys, evidence_content_hash: Optional[str]) -> str:
    """Stable hash over the facts that matter, excluding volatile timestamps.

    If ``evidence_content_hash`` is supplied (the real hash of the
    underlying stored payload, e.g. from ``HistoryStore.get_timeline_payload``)
    it is folded in so the manifest entry changes if and only if the
    underlying evidence actually changes.
    """
    payload = {
        "game_id": game_id,
        "source": source,
        "capability_flags": dict(sorted(capability_flags.items())),
        "completeness": round(float(completeness), 6),
        "game_metadata": game_metadata.to_dict(),
        "leakage": leakage.to_dict(),
        "evidence_content_hash": evidence_content_hash,
    }
    return sha256_hex(canonical_json(payload))


def build_entry(
        *, game_id: int, source: str, capture_method: str,
        player_group_keys: Iterable[str], completeness: float,
        privacy_classification: str, consent_status: str,
        game_metadata: Optional[GameMetadata] = None,
        champion: Optional[str] = None, role: Optional[str] = None,
        match_group_key: Optional[str] = None,
        capability_overrides: Optional[Mapping[str, bool]] = None,
        evidence_content_hash: Optional[str] = None,
        provenance_notes: Iterable[str] = (),
        collection_tool_version: str = "1",
        source_schema_version: str = "1",
        now: Optional[datetime.datetime] = None) -> ManifestEntry:
    """Deterministically build and validate one :class:`ManifestEntry`.

    ``entry_id`` and ``content_hash`` are pure functions of the supplied
    facts (excluding ``created_at``/``captured_at``), so building the same
    entry twice from the same inputs always produces the same identifiers.
    """
    if source not in EVIDENCE_SOURCES:
        raise ManifestValidationError(f"Unknown evidence source '{source}'")
    flags = dict(DEFAULT_CAPABILITY_FLAGS[source])
    if capability_overrides:
        flags.update(capability_overrides)
    metadata = game_metadata or GameMetadata()
    leakage = LeakageKeys(
        match_group_key=match_group_key or str(game_id),
        player_group_keys=tuple(sorted(player_group_keys)),
        champion=champion, role=role,
        region=metadata.region, rank_tier=metadata.rank_tier,
        patch=metadata.patch,
    )
    content_hash = _fact_hash(
        game_id, source, flags, completeness, metadata, leakage,
        evidence_content_hash,
    )
    entry_id = f"{game_id}:{source}"
    timestamp = (now or datetime.datetime.now(datetime.timezone.utc)).isoformat()
    entry = ManifestEntry(
        entry_id=entry_id,
        game_id=game_id,
        source=source,
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        provenance=Provenance(
            capture_method=capture_method,
            collection_tool_version=collection_tool_version,
            source_schema_version=source_schema_version,
            captured_at=timestamp,
            notes=tuple(provenance_notes),
        ),
        content_hash=content_hash,
        capability_flags=flags,
        completeness=max(0.0, min(1.0, float(completeness))),
        privacy_classification=privacy_classification,
        consent_status=consent_status,
        game_metadata=metadata,
        leakage=leakage,
        created_at=timestamp,
    )
    validate_entry(entry)
    return entry


def validate_entry(entry: ManifestEntry) -> None:
    if entry.source not in EVIDENCE_SOURCES:
        raise ManifestValidationError(f"Unknown evidence source '{entry.source}'")
    if entry.privacy_classification not in PRIVACY_CLASSIFICATIONS:
        raise ManifestValidationError(
            f"Unknown privacy classification '{entry.privacy_classification}'"
        )
    if entry.consent_status not in CONSENT_STATUSES:
        raise ManifestValidationError(
            f"Unknown consent status '{entry.consent_status}'"
        )
    if not (0.0 <= entry.completeness <= 1.0):
        raise ManifestValidationError("completeness must be within [0.0, 1.0]")
    flag_names = set(entry.capability_flags.keys())
    if flag_names != set(CAPABILITY_FLAG_NAMES):
        raise ManifestValidationError(
            "capability_flags must exactly cover "
            f"{sorted(CAPABILITY_FLAG_NAMES)}, got {sorted(flag_names)}"
        )
    for name, value in entry.capability_flags.items():
        if not isinstance(value, bool):
            raise ManifestValidationError(
                f"capability_flags['{name}'] must be a bool"
            )
    if entry.game_id <= 0:
        raise ManifestValidationError("game_id must be a positive integer")
    if not entry.leakage.player_group_keys:
        raise ManifestValidationError(
            "leakage.player_group_keys must not be empty"
        )
    problems = scan_for_forbidden(entry.to_dict())
    if problems:
        raise ManifestValidationError(
            "entry contains disallowed credential/identifier-shaped data: "
            + "; ".join(problems)
        )


class CorpusManifest:
    """An in-memory, file-backed collection of :class:`ManifestEntry`.

    Not thread-safe; intended for single-process batch building and CLI use.
    """

    def __init__(self) -> None:
        self._entries: dict[str, ManifestEntry] = {}

    def add_entry(self, entry: ManifestEntry, *, allow_overwrite: bool = False) -> None:
        validate_entry(entry)
        existing = self._entries.get(entry.entry_id)
        if existing is not None and existing.content_hash != entry.content_hash:
            if not allow_overwrite:
                raise ManifestValidationError(
                    f"Entry '{entry.entry_id}' already exists with a "
                    "different content hash; pass allow_overwrite=True to "
                    "replace it explicitly."
                )
        self._entries[entry.entry_id] = entry

    def get(self, entry_id: str) -> Optional[ManifestEntry]:
        return self._entries.get(entry_id)

    def to_list(self) -> list[ManifestEntry]:
        return [self._entries[key] for key in sorted(self._entries)]

    def filter_by(
            self, *, source: Optional[str] = None,
            privacy_classification: Optional[str] = None) -> list[ManifestEntry]:
        out = self.to_list()
        if source is not None:
            out = [e for e in out if e.source == source]
        if privacy_classification is not None:
            out = [e for e in out if e.privacy_classification == privacy_classification]
        return out

    def stats(self) -> dict:
        entries = self.to_list()
        by_source: dict[str, int] = {}
        for entry in entries:
            by_source[entry.source] = by_source.get(entry.source, 0) + 1
        return {
            "total_entries": len(entries),
            "by_source": by_source,
            "mean_completeness": (
                round(sum(e.completeness for e in entries) / len(entries), 4)
                if entries else 0.0
            ),
        }

    def save(self, path) -> None:
        data = [entry.to_dict() for entry in self.to_list()]
        Path(path).write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8",
        )

    @classmethod
    def load(cls, path) -> "CorpusManifest":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        manifest = cls()
        for raw in data:
            manifest.add_entry(ManifestEntry.from_dict(raw))
        return manifest


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Corpus manifest utilities")
    sub = parser.add_subparsers(dest="command", required=True)

    validate_parser = sub.add_parser("validate", help="Validate a manifest file")
    validate_parser.add_argument("path")

    stats_parser = sub.add_parser("stats", help="Print manifest summary stats")
    stats_parser.add_argument("path")

    args = parser.parse_args()
    if args.command == "validate":
        try:
            manifest = CorpusManifest.load(args.path)
        except ManifestValidationError as exc:
            print(f"INVALID: {exc}")
            return 1
        print(f"OK: {len(manifest.to_list())} entries validated")
        return 0
    if args.command == "stats":
        manifest = CorpusManifest.load(args.path)
        print(json.dumps(manifest.stats(), indent=2, sort_keys=True))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
