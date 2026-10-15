"""Declarative exclusion lists (LD6, additive-only, no migration).

Governance-as-config: named exclusion entries applied as anti-joins at
load. Each entry MUST carry reason + actor + timestamp — bare IDs in
config (legacy's `customers_exclusion: [15300]`) are rejected at load.

Entries live in `pipelines/exclusions.yaml` (empty by default: zero
behavior change). Kinds key on the record's natural key:
  customer -> member_id, order -> external_order_id,
  event    -> source_event_id.
"""
from __future__ import annotations

EXCLUSION_KINDS = ("customer", "order", "event")


class ExcludedError(ValueError):
    """Raised when a record matches an exclusion entry.

    Subclasses ValueError so existing reject paths treat it as a
    reject (never a silent pass); carries the entry for audit logging.
    Loaders should catch it separately to skip+count rather than
    quarantine (excluded != invalid).
    """

    def __init__(self, kind: str, value: str, entry: dict):
        super().__init__(f"excluded {kind} {value!r}: {entry.get('reason')}")
        self.kind = kind
        self.value = value
        self.entry = entry


def validate_entry(entry: dict, index: int) -> dict:
    """Validate one exclusion entry. Raises ValueError naming the gap."""
    if not isinstance(entry, dict):
        raise ValueError(f"exclusions[{index}]: entry must be a mapping")
    kind = entry.get("kind")
    if kind not in EXCLUSION_KINDS:
        raise ValueError(f"exclusions[{index}]: unknown kind {kind!r} "
                         f"(expected one of {EXCLUSION_KINDS})")
    for field in ("value", "reason", "actor", "created_at"):
        if not entry.get(field):
            raise ValueError(f"exclusions[{index}]: {field!r} is required "
                             f"(bare-ID entries are rejected)")
    return {"kind": kind, "value": str(entry["value"]),
            "reason": entry["reason"], "actor": entry["actor"],
            "created_at": entry["created_at"]}


def load_exclusions(raw: list | None) -> list[dict]:
    """Validate a raw exclusions list (parsed YAML). Empty/None -> []."""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError("exclusions: top level must be a list")
    return [validate_entry(e, i) for i, e in enumerate(raw)]


def find_exclusion(exclusions: list[dict], kind: str,
                   value: str | None) -> dict | None:
    """Return the matching entry, or None. None value never matches."""
    if value is None:
        return None
    sval = str(value)
    for entry in exclusions:
        if entry["kind"] == kind and entry["value"] == sval:
            return entry
    return None


def check_excluded(exclusions: list[dict], kind: str,
                   value: str | None) -> None:
    """Raise ExcludedError on match; no-op otherwise (incl. empty list)."""
    entry = find_exclusion(exclusions, kind, value)
    if entry is not None:
        raise ExcludedError(kind, str(value), entry)
