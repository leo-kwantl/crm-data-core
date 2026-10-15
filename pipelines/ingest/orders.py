"""Order ingest: validate store, normalize lines, upsert idempotently.

Contract (mirrors crm-cdp-app `orders` + `db/queries/orders.sql`):
- `store_id` NOT NULL — resolve or create the store row first.
- Idempotency key: source `external_order_id` (unique per source system).
- Money: integer cents only; no floats past the boundary.

Stages: extract (source client) -> normalize (this module) -> load
(validated upsert into `orders` + `order_lines`). CDC/outbox to follow.
"""
from __future__ import annotations


def normalize(raw: dict, exclusions: list = ()) -> dict:
    """Validate + normalize one raw source order. Raises ValueError."""
    from pipelines.exclusions import check_excluded
    check_excluded(exclusions, "order", raw.get("external_order_id"))
    check_excluded(exclusions, "customer", raw.get("member_id"))
    if not raw.get("store_id"):
        raise ValueError("store_id is required (NOT NULL)")
    if not raw.get("external_order_id"):
        raise ValueError("external_order_id is required (idempotency key)")
    lines = raw.get("lines") or []
    if not lines:
        raise ValueError("order must have >= 1 line")
    total = 0
    norm_lines = []
    for ln in lines:
        qty = int(ln.get("qty", 0))
        unit = int(ln.get("unit_cents", 0))
        if qty < 1 or unit < 0:
            raise ValueError(f"bad line: {ln!r}")
        total += qty * unit
        norm_lines.append({"sku": ln.get("sku"), "qty": qty, "unit_cents": unit})
    return {
        "store_id": raw["store_id"],
        "external_order_id": raw["external_order_id"],
        "customer_id": raw.get("customer_id"),
        "lines": norm_lines,
        "total_cents": total,
    }
