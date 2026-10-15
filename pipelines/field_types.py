"""Canonical field-type table (LD10, additive-only, no migration).

Declares the canonical type of every core ingest field per entity so D1
`column_map` authors and connector builders can validate mappings BEFORE
rows hit the pipeline. This is a VALIDATION AID ONLY: it never coerces
and never rejects. Reject+quarantine stays authoritative (especially
for money/identity) — a mismatch here produces an advisory string, and
the row still goes through normal validation.

Kinds: text | integer | numeric | boolean | timestamptz | uuid | jsonb.
Unknown fields are ignored (custom fields belong in LD1 side tables).
"""
from __future__ import annotations

from datetime import datetime

FIELD_TYPES: dict[str, dict[str, str]] = {
    "customers": {
        "member_id": "text",
        "full_name": "text",
        "external_member_id": "text",
        "phone": "text",
        "email": "text",
    },
    "orders": {
        "store_id": "text",
        "external_order_id": "text",
        "customer_id": "text",
        "member_id": "text",
        "external_member_id": "text",
        "total_cents": "integer",
        "status": "text",
        "ordered_at": "timestamptz",
    },
    "events": {
        "customer_id": "text",
        "member_id": "text",
        "external_member_id": "text",
        "type": "text",
        "source": "text",
        "source_event_id": "text",
        "summary": "text",
        "payload": "jsonb",
        "occurred_at": "timestamptz",
    },
    "identity": {
        "member_id": "text",
        "external_member_id": "text",
        "source": "text",
    },
    "products": {
        "sku": "text",
        "name": "text",
        "category": "text",
        "price_cents": "integer",
    },
}


def _matches(kind: str, value: object) -> bool:
    if value is None:
        return True  # NULLs are a NOT-NULL question, not a type question
    if kind == "text":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "numeric":
        return (isinstance(value, (int, float))
                and not isinstance(value, bool))
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "timestamptz":
        return isinstance(value, (datetime, str))
    if kind == "uuid":
        return isinstance(value, str)
    if kind == "jsonb":
        return isinstance(value, (dict, list, str))
    raise ValueError(f"unknown canonical kind: {kind!r}")


def type_mismatches(entity: str, record: dict) -> list[str]:
    """Advisory type check. Returns mismatch strings, else [].

    Raises ValueError on unknown entity. Never raises on the record:
    unknown fields ignored, NULLs skipped.
    """
    if entity not in FIELD_TYPES:
        raise ValueError(f"unknown entity: {entity!r} "
                         f"(expected one of {sorted(FIELD_TYPES)})")
    spec = FIELD_TYPES[entity]
    out: list[str] = []
    for field, kind in spec.items():
        if field in record and not _matches(kind, record[field]):
            out.append(f"{entity}.{field}: want {kind}, "
                       f"got {type(record[field]).__name__}")
    return out
