"""Order ingest: validate store, normalize lines, upsert idempotently.

Contract (mirrors crm-cdp-app `orders` + `db/queries/orders.sql`):
- `store_id` NOT NULL — resolve or create the store row first.
- Idempotency key: source `external_order_id` (unique per source system).
- Money: integer cents only; no floats past the boundary.

Stages: extract (source client) -> normalize (this module) -> load
(validated upsert into `orders` + `order_lines`). CDC/outbox to follow.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_CENTS = Decimal("1")


def decimal_number(value, *, field: str) -> Decimal:
    """Feed number -> Decimal, exactly. Raises ValueError (quarantine)."""
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field}: not a number: {value!r}") from None
    if not dec.is_finite():
        raise ValueError(f"{field}: not a finite number: {value!r}")
    return dec


def money_cents(value, *, field: str, scale: int = 1) -> int:
    """Money -> integer cents, exact: no float in the path.

    A `*_cents` field is already cents (scale 1); a plain money field is
    currency units (scale 100). Half a cent rounds up: `Decimal('2.675') *
    100` is exactly `267.5`, where `float('2.675') * 100` is
    `267.49999999999997` and rounded down to 267 -- a cent lost per line,
    which no later reader can tell from a real price.
    """
    cents = decimal_number(value, field=field) * Decimal(scale)
    return int(cents.quantize(_CENTS, rounding=ROUND_HALF_UP))


def whole_number(value, *, field: str) -> int:
    """Feed count -> int, refusing a fraction rather than truncating it."""
    dec = decimal_number(value, field=field)
    if dec != dec.to_integral_value():
        raise ValueError(f"{field}: must be a whole number, got {value!r}")
    return int(dec)


def _declared_total_cents(raw: dict):
    """The feed's own order total if it carries one, else None."""
    val = raw.get("total_cents")
    if val is None or str(val).strip() == "":
        return None
    return whole_number(val, field="total_cents")


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
        qty = whole_number(ln.get("qty", 0), field="qty")
        unit = whole_number(ln.get("unit_cents", 0), field="unit_cents")
        if qty < 1 or unit < 0:
            raise ValueError(f"bad line: {ln!r}")
        total += qty * unit
        norm_lines.append({"sku": ln.get("sku"), "qty": qty, "unit_cents": unit})
    declared = _declared_total_cents(raw)
    if declared is not None and declared != total:
        # A discount, shipping or tax line is exactly this shape. Choosing
        # one of the two answers silently is how a revenue figure stops
        # being auditable, so the row is refused with both numbers named.
        raise ValueError(
            f"total_cents {declared} does not match the line sum {total}")
    return {
        "store_id": raw["store_id"],
        "external_order_id": raw["external_order_id"],
        "customer_id": raw.get("customer_id"),
        "lines": norm_lines,
        "total_cents": total if declared is None else declared,
    }
