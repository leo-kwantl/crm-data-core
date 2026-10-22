"""Raw-SQL customer repository (no ORM). All SQL mirrors db/queries/customers.sql."""

from __future__ import annotations

import base64
import json
import random
from datetime import datetime
from uuid import UUID

import asyncpg

from ..schemas.customer import (
    normalize_address_line,
    normalize_contact,
    normalize_date_of_birth,
    normalize_district,
    normalize_external_member_id,
    normalize_gender,
)
from . import consents as consents_repo
from . import search as search_repo

CHANNELS = ("sms", "email", "whatsapp")


class DuplicateContactError(Exception):
    pass


class DuplicateExternalIdError(Exception):
    pass


def _contacts_in(payload: list[dict]) -> list[dict]:
    """Validate + normalize contact inputs. Raises ValueError on bad format."""
    out = []
    for c in payload:
        kind = c["kind"] if isinstance(c, dict) else c.kind
        value = c["value"] if isinstance(c, dict) else c.value
        primary = c.get("is_primary", False) if isinstance(c, dict) else c.is_primary
        normalized = normalize_contact(kind, value)
        out.append(
            {
                "kind": kind,
                "value_normalized": normalized,
                "value_display": value.strip(),
                "is_primary": bool(primary),
            }
        )
    return out


def _generate_member_id() -> str:
    return f"M-{random.randint(0, 999999):06d}"


# The shared statements of db/queries/customers.sql, copied verbatim: each
# runtime copy carries the file's `-- name:` marker and is token-equal to
# it (tests/test_shared_statement_guard.py), so the two cannot drift.

CUSTOMER_BY_ID_SQL = """
-- name: customer_by_id
-- params: customer_id uuid
SELECT id, member_id, external_member_id, full_name, attributes,
  created_at, updated_at, group_id, linked_customer_id,
  gender, date_of_birth, district, address_line,
  date_part('year', age(date_of_birth))::int AS age_years
FROM customers
WHERE id = $1 AND deleted_at IS NULL;
"""

CUSTOMER_CONTACTS_SQL = """
-- name: customer_contacts
-- params: customer_id uuid
SELECT id, customer_id, kind, value_normalized, value_display, is_primary, created_at
FROM customer_contacts
WHERE customer_id = $1
ORDER BY created_at;
"""

CUSTOMER_CONSENTS_SQL = """
-- name: customer_consents
-- params: customer_id uuid
SELECT channel, status
FROM consents
WHERE customer_id = $1;
"""

CUSTOMER_PURCHASE_SUMMARY_SQL = """
-- name: customer_purchase_summary
-- params: customer_id uuid
SELECT COUNT(*) AS order_count,
       COALESCE(SUM(total_cents), 0) AS total_spent_cents,
       MAX(ordered_at) AS last_order_at
FROM orders
WHERE customer_id = $1;
"""

CUSTOMER_INSERT_SQL = """
-- name: customer_insert
-- params: member_id text, external_member_id text|None, full_name text, attributes jsonb,
--   group_id uuid|None, gender text|None, date_of_birth date|None, district text|None, address_line text|None
INSERT INTO customers (member_id, external_member_id, full_name, attributes,
  group_id, gender, date_of_birth, district, address_line)
VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9)
RETURNING id, member_id, external_member_id, full_name, attributes,
  created_at, updated_at;
"""

CUSTOMER_CONTACT_INSERT_SQL = """
-- name: customer_contact_insert
-- params: customer_id uuid, kind text, value_normalized text, value_display text, is_primary bool
INSERT INTO customer_contacts (customer_id, kind, value_normalized, value_display, is_primary)
VALUES ($1, $2, $3, $4, $5);
"""

CUSTOMER_UPDATE_NAME_SQL = """
-- name: customer_update_name
-- params: customer_id uuid, full_name text
UPDATE customers SET full_name = $2, updated_at = now()
WHERE id = $1 AND deleted_at IS NULL;
"""

CUSTOMER_MERGE_ATTRIBUTES_SQL = """
-- name: customer_merge_attributes
-- params: customer_id uuid, attributes jsonb
UPDATE customers
SET attributes = jsonb_strip_nulls(COALESCE(attributes, '{}'::jsonb) || $2::jsonb),
    updated_at = now()
WHERE id = $1 AND deleted_at IS NULL;
"""

CUSTOMER_REPLACE_CONTACTS_DELETE_SQL = """
-- name: customer_replace_contacts_delete
-- params: customer_id uuid
DELETE FROM customer_contacts WHERE customer_id = $1;
"""

CUSTOMER_SOFT_DELETE_SQL = """
-- name: customer_soft_delete
-- params: customer_id uuid
UPDATE customers SET deleted_at = now(), updated_at = now()
WHERE id = $1 AND deleted_at IS NULL;
"""


async def group_of_customer(
    pool: asyncpg.Pool, customer_id: UUID
) -> str | None:
    """Owning group id (str) of a live customer, or None when missing/ungrouped.

    Customer-touching routers 404 when this falls outside the session group
    (never leak cross-group existence); linked_customer_id stays NULL by
    default — no auto-merge/matching logic writes it anywhere."""
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT group_id FROM customers WHERE id = $1 AND deleted_at IS NULL",
            customer_id,
        )
        return str(row) if row is not None else None


async def create_customer(
    pool: asyncpg.Pool,
    *,
    full_name: str,
    contacts: list[dict],
    attributes: dict,
    external_member_id: str | None = None,
    group_id: UUID | None = None,
    gender: str | None = None,
    date_of_birth=None,
    district: str | None = None,
    address_line: str | None = None,
) -> dict:
    """Insert customer + contacts. Retries member_id on collision. Returns profile dict.

    Every row lands in a group (explicit, else the DEMO holder); the holder
    lookup raises LookupError on a fresh DB without migrations."""
    normalized = _contacts_in(contacts)
    ext_id = normalize_external_member_id(external_member_id)
    gender = normalize_gender(gender)
    dob = normalize_date_of_birth(date_of_birth)
    district = normalize_district(district)
    address_line = normalize_address_line(address_line)
    attrs_json = json.dumps(attributes or {})
    async with pool.acquire() as conn:
        if group_id is None:
            holder = await conn.fetchval(
                "SELECT id FROM groups WHERE code = 'DEMO'"
            )
            if holder is None:
                raise LookupError("no_default_group")
            group_id = holder
        for _ in range(10):
            member_id = _generate_member_id()
            try:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        CUSTOMER_INSERT_SQL,
                        member_id,
                        ext_id,
                        full_name,
                        attrs_json,
                        group_id,
                        gender,
                        dob,
                        district,
                        address_line,
                    )
                    for c in normalized:
                        try:
                            await conn.execute(
                                CUSTOMER_CONTACT_INSERT_SQL,
                                row["id"],
                                c["kind"],
                                c["value_normalized"],
                                c["value_display"],
                                c["is_primary"],
                            )
                        except asyncpg.UniqueViolationError:
                            raise DuplicateContactError(c["value_normalized"])
                    return await _build_profile(conn, row["id"])
            except asyncpg.UniqueViolationError as exc:
                msg = str(exc).lower()
                if "member_id" in msg and "external" not in msg:
                    continue
                if "external_member_id" in msg:
                    raise DuplicateExternalIdError(str(exc))
                raise DuplicateContactError(str(exc))
        raise RuntimeError("could not allocate member_id")


async def _build_profile(conn: asyncpg.Connection, customer_id: UUID) -> dict | None:
    row = await conn.fetchrow(
        CUSTOMER_BY_ID_SQL,
        customer_id,
    )
    if row is None:
        return None
    contact_rows = await conn.fetch(
        CUSTOMER_CONTACTS_SQL,
        customer_id,
    )
    consent_rows = await conn.fetch(CUSTOMER_CONSENTS_SQL, customer_id)
    rollup = await conn.fetchrow(
        CUSTOMER_PURCHASE_SUMMARY_SQL,
        customer_id,
    )
    status_map: dict[str, str | None] = {ch: None for ch in CHANNELS}
    for r in consent_rows:
        status_map[r["channel"]] = r["status"]
    attrs = row["attributes"]
    if isinstance(attrs, str):
        attrs = json.loads(attrs)
    dob = row["date_of_birth"]
    return {
        "id": row["id"],
        "member_id": row["member_id"],
        "external_member_id": row["external_member_id"],
        "full_name": row["full_name"],
        "gender": row["gender"],
        "date_of_birth": dob.isoformat() if dob is not None else None,
        "age_years": row["age_years"],
        "district": row["district"],
        "address_line": row["address_line"],
        "group_id": row["group_id"],
        "linked_customer_id": row["linked_customer_id"],
        "attributes": attrs or {},
        "contacts": [dict(r) for r in contact_rows],
        "subscription_status": status_map,
        "preference_summary": dict(attrs or {}),
        "purchase_summary": {
            "order_count": int(rollup["order_count"] or 0),
            "total_spent_cents": int(rollup["total_spent_cents"] or 0),
            "last_order_at": rollup["last_order_at"],
        },
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def get_customer(pool: asyncpg.Pool, customer_id: UUID) -> dict | None:
    async with pool.acquire() as conn:
        return await _build_profile(conn, customer_id)


async def update_customer(
    pool: asyncpg.Pool,
    customer_id: UUID,
    *,
    full_name: str | None = None,
    attributes: dict | None = None,
    contacts: list[dict] | None = None,
    external_member_id: str | None = None,
    external_member_id_set: bool = False,
    gender: str | None = None,
    gender_set: bool = False,
    date_of_birth=None,
    date_of_birth_set: bool = False,
    district: str | None = None,
    district_set: bool = False,
    address_line: str | None = None,
    address_line_set: bool = False,
) -> dict | None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval(
                "SELECT id FROM customers WHERE id = $1 AND deleted_at IS NULL", customer_id
            )
            if exists is None:
                return None
            if full_name is not None:
                await conn.execute(
                    CUSTOMER_UPDATE_NAME_SQL,
                    customer_id,
                    full_name,
                )
            if attributes is not None:
                await conn.execute(
                    CUSTOMER_MERGE_ATTRIBUTES_SQL,
                    customer_id,
                    json.dumps(attributes),
                )
            if external_member_id_set:
                ext_id = normalize_external_member_id(external_member_id)
                try:
                    await conn.execute(
                        "UPDATE customers SET external_member_id = $2, updated_at = now()"
                        " WHERE id = $1 AND deleted_at IS NULL",
                        customer_id,
                        ext_id,
                    )
                except asyncpg.UniqueViolationError as exc:
                    raise DuplicateExternalIdError(str(exc))
            if gender_set:
                await conn.execute(
                    "UPDATE customers SET gender = $2, updated_at = now()"
                    " WHERE id = $1 AND deleted_at IS NULL",
                    customer_id,
                    normalize_gender(gender),
                )
            if date_of_birth_set:
                await conn.execute(
                    "UPDATE customers SET date_of_birth = $2, updated_at = now()"
                    " WHERE id = $1 AND deleted_at IS NULL",
                    customer_id,
                    normalize_date_of_birth(date_of_birth),
                )
            if district_set:
                await conn.execute(
                    "UPDATE customers SET district = $2, updated_at = now()"
                    " WHERE id = $1 AND deleted_at IS NULL",
                    customer_id,
                    normalize_district(district),
                )
            if address_line_set:
                await conn.execute(
                    "UPDATE customers SET address_line = $2, updated_at = now()"
                    " WHERE id = $1 AND deleted_at IS NULL",
                    customer_id,
                    normalize_address_line(address_line),
                )
            if contacts is not None:
                normalized = _contacts_in(contacts)
                await conn.execute(
                    CUSTOMER_REPLACE_CONTACTS_DELETE_SQL, customer_id
                )
                for c in normalized:
                    try:
                        await conn.execute(
                            CUSTOMER_CONTACT_INSERT_SQL,
                            customer_id,
                            c["kind"],
                            c["value_normalized"],
                            c["value_display"],
                            c["is_primary"],
                        )
                    except asyncpg.UniqueViolationError:
                        raise DuplicateContactError(c["value_normalized"])
            return await _build_profile(conn, customer_id)


async def soft_delete_customer(pool: asyncpg.Pool, customer_id: UUID) -> bool:
    """Set deleted_at; return True if a live row was hidden."""
    async with pool.acquire() as conn:
        res = await conn.execute(
            CUSTOMER_SOFT_DELETE_SQL,
            customer_id,
        )
        return res.split()[-1] == "1"


# --- Global masked list (mirrors db/queries/customers.sql: list_customers + page_*) ---
#
# BRAND SCOPE (7.2-2): the three statements below carry an extra $7/$4/$6 brand
# predicate that db/queries/customers.sql — token-equal with crm-cdp-data, see
# tests/test_mirror_sql.py — does not. This file is the app's serving copy and
# the app must not serve a group-wide customer list to a brand-scoped session;
# the shared file keeps the unscoped canonical form because it is data's
# ETL-side statement. Folding the predicate into the shared copy is a row-
# visibility change (BREAKING for data), announced to crm-data 2026-10-19; until
# they land their half, editing either side here means editing the predicate in
# this literal too. The rule for a BRAND-SCOPED session is "belongs to this
# brand, or belongs to nobody yet": a completed order in the brand, OR no
# completed order anywhere (fresh lead / pre-brand row).
#
# A NULL brand ($7 IS NULL) is NOT that rule narrowed to the unattributed
# rows -- `$7 IS NULL OR ...` makes the disjunction TRUE, so the brand filter
# disappears entirely. That is deliberate and matches deps.user_brand_id
# (None for group-wide roles) and require_customer_in_scope, which returns
# early for the same session: list, search and read agree on "unpinned".
# Narrowing only the list here would make read and list disagree, and the
# group filter is disabled the same way when $6 IS NULL.
# tests/test_brand_isolation.py::test_brand_less_session_sees_all_customers
# pins it, and PATCH /staff/{id} is require_admin (routers/identity.py), so a
# non-admin cannot reach the brand-less state to begin with.

LIST_CUSTOMERS_SQL = """
-- name: list_customers
-- params: cursor_at timestamptz|None, cursor_id uuid|None, limit int, tier_code text|None, sms_consent text|None, group_id uuid|None, brand_id uuid|None
-- Masked list, newest-first on (created_at DESC, id DESC).
-- Keyset page: fetch limit+1 upstream. Contacts are masked at
-- serialization (see repos/customers.list_customers_page); never LIKE on PII.
-- SCOPE: brand_id is the session's own brand (deps.session_brand); a customer
-- with no completed order is visible to every brand.
SELECT id, member_id, external_member_id, full_name, tier_code, created_at,
  gender, date_part('year', age(date_of_birth))::int AS age_years
FROM customers
WHERE deleted_at IS NULL
  AND ($4::text IS NULL OR tier_code = $4)
  AND ($5::text IS NULL OR EXISTS
    (SELECT 1 FROM consents s WHERE s.customer_id = customers.id
     AND s.channel = 'sms' AND s.status = $5))
  AND ($1::timestamptz IS NULL OR (created_at, id) < ($1::timestamptz, $2::uuid))
  AND ($6::uuid IS NULL OR group_id = $6::uuid OR group_id IS NULL)
  AND ($7::uuid IS NULL OR EXISTS
    (SELECT 1 FROM orders o JOIN stores s ON s.id = o.store_id
     WHERE o.customer_id = customers.id AND o.status = 'completed'
     AND s.brand_id = $7::uuid)
    OR NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = customers.id
     AND o.status = 'completed'))
ORDER BY created_at DESC, id DESC
LIMIT $3;
"""

PAGE_CONTACTS_SQL = """
-- name: page_contacts
-- params: customer_ids uuid[]
SELECT id, customer_id, kind, value_normalized, value_display, is_primary, created_at
FROM customer_contacts
WHERE customer_id = ANY($1::uuid[])
ORDER BY created_at;
"""

PAGE_ORDER_COUNTS_SQL = """
-- name: page_order_counts
-- params: customer_ids uuid[]
SELECT customer_id, COUNT(*) AS order_count
FROM orders
WHERE customer_id = ANY($1::uuid[])
GROUP BY customer_id;
"""

PAGE_CONSENTS_SQL = """
-- name: page_consents
-- params: customer_ids uuid[]
SELECT customer_id, channel, status
FROM consents
WHERE customer_id = ANY($1::uuid[]);
"""

COUNT_CUSTOMERS_SQL = """
-- name: count_customers
-- params: tier_code text|None, sms_consent text|None, group_id uuid|None, brand_id uuid|None
-- The scoped twin of list_customers: the total for the same filters.
SELECT COUNT(*) FROM customers
WHERE deleted_at IS NULL
  AND ($1::text IS NULL OR tier_code = $1)
  AND ($2::text IS NULL OR EXISTS
    (SELECT 1 FROM consents s WHERE s.customer_id = customers.id
     AND s.channel = 'sms' AND s.status = $2))
  AND ($3::uuid IS NULL OR group_id = $3::uuid OR group_id IS NULL)
  AND ($4::uuid IS NULL OR EXISTS
    (SELECT 1 FROM orders o JOIN stores s ON s.id = o.store_id
     WHERE o.customer_id = customers.id AND o.status = 'completed'
     AND s.brand_id = $4::uuid)
    OR NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = customers.id
     AND o.status = 'completed'));
"""

OFFSET_CUSTOMERS_SQL = """
SELECT id, member_id, external_member_id, full_name, tier_code, created_at,
  gender, date_part('year', age(date_of_birth))::int AS age_years
FROM customers
WHERE deleted_at IS NULL
  AND ($3::text IS NULL OR tier_code = $3)
  AND ($4::text IS NULL OR EXISTS
    (SELECT 1 FROM consents s WHERE s.customer_id = customers.id
     AND s.channel = 'sms' AND s.status = $4))
  AND ($5::uuid IS NULL OR group_id = $5::uuid OR group_id IS NULL)
  AND ($6::uuid IS NULL OR EXISTS
    (SELECT 1 FROM orders o JOIN stores s ON s.id = o.store_id
     WHERE o.customer_id = customers.id AND o.status = 'completed'
     AND s.brand_id = $6::uuid)
    OR NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = customers.id
     AND o.status = 'completed'))
ORDER BY created_at DESC, id DESC
LIMIT $1 OFFSET $2
"""


def normalize_filters(
    tier_code: str | None, sms_status: str | None
) -> tuple[str | None, str | None]:
    """Clean additive list filters. Raises ValueError on a bad sms_status."""
    tier = tier_code.strip().upper() if tier_code and tier_code.strip() else None
    sms = sms_status.strip().lower() if sms_status and sms_status.strip() else None
    if sms is not None and sms not in ("opted_in", "opted_out"):
        raise ValueError("invalid_sms_status")
    return tier, sms


def encode_customer_cursor(at: datetime, row_id: UUID) -> str:
    raw = json.dumps({"at": at.isoformat(), "id": str(row_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_customer_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Return (created_at, id). Raises ValueError on malformed cursors."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(payload["at"]), UUID(payload["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_cursor") from exc


async def list_customers_page(
    conn: asyncpg.Connection,
    *,
    limit: int,
    cursor: str | None,
    tier_code: str | None = None,
    sms_status: str | None = None,
    group_id: UUID | None = None,
    brand_id: UUID | None = None,
) -> tuple[list[dict], str | None]:
    """Newest-first page of customer summaries with FULL contacts (staff override
    2026-10-08: staff MUST see full values; mask helpers retained but unused).

    Each item: id, member_id, full_name, masked_contact (primary FULL value,
    contacts (full copies), order_count, sms_status, contactable, created_at.
    Returns (items, next_cursor); caller fetches limit+1.
    """
    cursor_at: datetime | None = None
    cursor_id: UUID | None = None
    if cursor:
        cursor_at, cursor_id = decode_customer_cursor(cursor)
    tier, sms = normalize_filters(tier_code, sms_status)
    rows = await conn.fetch(
        LIST_CUSTOMERS_SQL, cursor_at, cursor_id, limit + 1, tier, sms, group_id,
        brand_id,
    )
    page = [dict(r) for r in rows]
    next_cursor = None
    if len(page) > limit:
        page.pop()  # overflow row, only proves a next page exists
        last = page[-1]
        next_cursor = encode_customer_cursor(last["created_at"], last["id"])
    if not page:
        return [], None
    items = await enrich_customer_page(conn, page)
    return items, next_cursor


async def enrich_customer_page(
    conn: asyncpg.Connection, page: list[dict]
) -> list[dict]:
    """Attach contacts/order counts/consents to raw customer list rows."""
    ids = [r["id"] for r in page]
    contact_rows = await conn.fetch(PAGE_CONTACTS_SQL, ids)
    count_rows = await conn.fetch(PAGE_ORDER_COUNTS_SQL, ids)
    consent_rows = await conn.fetch(PAGE_CONSENTS_SQL, ids)
    by_contact: dict[str, list[dict]] = {}
    for cr in contact_rows:
        row = dict(cr)
        by_contact.setdefault(str(row["customer_id"]), []).append(row)
    by_count = {str(r["customer_id"]): int(r["order_count"]) for r in count_rows}
    by_consent: dict[str, list[dict]] = {}
    for cr in consent_rows:
        row = dict(cr)
        by_consent.setdefault(str(row["customer_id"]), []).append(row)
    items: list[dict] = []
    for r in page:
        cid = str(r["id"])
        raw_contacts = by_contact.get(cid, [])
        full = [dict(c) for c in raw_contacts]
        primary = next(
            (c for c in full if c.get("is_primary")),
            full[0] if full else None,
        )
        consents = by_consent.get(cid, [])
        sms_status = next(
            (c["status"] for c in consents if c.get("channel") == "sms"), None
        )
        items.append(
            {
                "id": cid,
                "member_id": r["member_id"],
                "external_member_id": r["external_member_id"],
                "full_name": r["full_name"],
                "tier_code": r["tier_code"],
                "gender": r.get("gender"),
                "age_years": r.get("age_years"),
                "masked_contact": primary["value_display"] if primary else None,
                "contacts": full,
                "order_count": by_count.get(cid, 0),
                "sms_status": sms_status,
                "contactable": consents_repo.derive_contactability(consents),
                "created_at": r["created_at"],
            }
        )
    return items


async def list_customers_offset(
    conn: asyncpg.Connection,
    *,
    page: int,
    page_size: int,
    tier_code: str | None = None,
    sms_status: str | None = None,
    group_id: UUID | None = None,
    brand_id: UUID | None = None,
) -> dict:
    """Numbered page (1-based) of enriched customer summaries.

    Same newest-first order as the cursor list; returns
    {data, page, page_size, total, total_pages}.
    """
    from .paging import offset_payload

    tier, sms = normalize_filters(tier_code, sms_status)
    total = await conn.fetchval(COUNT_CUSTOMERS_SQL, tier, sms, group_id, brand_id)
    rows = await conn.fetch(
        OFFSET_CUSTOMERS_SQL, page_size, (page - 1) * page_size, tier, sms,
        group_id, brand_id,
    )
    items = (
        await enrich_customer_page(conn, [dict(r) for r in rows]) if rows else []
    )
    return offset_payload(items, int(total or 0), page, page_size)
