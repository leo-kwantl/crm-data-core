"""Event ingest: append-only customer/consent events.

Contract: `customer_events` / consent tables are immutable history —
INSERT only, no UPDATE/DELETE grants (same pattern as `rfm_snapshots`).
Each event carries (customer_id, type, payload, occurred_at, source).
Ded provisions: (source, source_event_id) unique; replays are no-ops.
"""
from __future__ import annotations

from typing import NotRequired, TypedDict


class EventPayload(TypedDict):
    """The shape `normalize` hands `apply._apply_event`, read from both ends.

    `occurred_at` is optional on the wire and defaults to `now()` at the
    writer; `payload` is opaque JSON forwarded verbatim into the column.
    """

    customer_id: str
    type: str
    payload: dict
    occurred_at: NotRequired[str | None]
    source: str
    source_event_id: str


def normalize(raw: dict, exclusions: list = ()) -> EventPayload:
    """Validate + normalize one raw source event. Raises ValueError."""
    from pipelines.exclusions import check_excluded
    check_excluded(exclusions, "event", raw.get("source_event_id"))
    check_excluded(exclusions, "customer", raw.get("member_id"))
    if not raw.get("customer_id"):
        raise ValueError("customer_id is required")
    if not raw.get("type"):
        raise ValueError("event type is required")
    if not raw.get("source") or not raw.get("source_event_id"):
        raise ValueError("(source, source_event_id) required for dedup")
    return {
        "customer_id": raw["customer_id"],
        "type": raw["type"],
        "payload": raw.get("payload") or {},
        "occurred_at": raw.get("occurred_at"),
        "source": raw["source"],
        "source_event_id": raw["source_event_id"],
    }
