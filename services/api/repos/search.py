"""Search repository: pure SQL over asyncpg. Canonical SQL lives in
db/queries/customers_search.sql (parsed by -- name: markers).

PII rules:
- email matches exact normalized (lowercase) first, then a
  case-insensitive substring fallback for staff keyword search
  (needles under EMAIL_PARTIAL_MIN_CHARS match nothing).
- phone matches exact E.164 first, then falls back to a digit-substring
  LIKE over value_normalized (staff keyword search, e.g. last-4 "9021");
  needles under PHONE_PARTIAL_MIN_DIGITS digits match nothing.
- phone/email stay POST-body-only (never URL), auth + rate-limit + audit.
- Contacts are returned in FULL for staff (owner override 2026-10-08:
  staff MUST see full values in list + record UI). mask_* helpers are
  retained for potential future use + tests, but serialization never masks.
- Audit rows carry criteria_type + result_count, NEVER raw phone/email values.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

QUERIES_DIR = Path(__file__).resolve().parents[3] / "db" / "queries"
# Shared with data, byte-mirrored (test_mirror_sql.py): scope-less callers.
QUERIES_PATH = QUERIES_DIR / "customers_search.sql"
# APP-ONLY scoped variants of the four capped statements (finding #75):
# `LIMIT` must see the scoped set, and the shared file cannot carry app policy.
SCOPED_QUERIES_PATH = QUERIES_DIR / "customers_search_scoped.sql"

_NAME_RE = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.M)
NAME_LIMIT = 20
MEMBER_PARTIAL_LIMIT = 20
PHONE_PARTIAL_LIMIT = 20
EMAIL_PARTIAL_LIMIT = 20
# Digit-substring phone fallback needs at least this many digits: 1-2 digit
# needles would match a meaningless slice of the base (capped at the limit
# above). Mirrors the topbar fan-out gate in apps/web/lib/api.ts.
PHONE_PARTIAL_MIN_DIGITS = 3
# Email fallback needs at least this many chars: 1-2 char needles ("a@")
# would match a meaningless slice of the base (capped at the limit above).
EMAIL_PARTIAL_MIN_CHARS = 3


def phone_digits(raw: str | None) -> str:
    """Digits-only needle for the phone-partial fallback (LIKE-safe: no
    %, _, or backslash can survive stripping)."""
    return re.sub(r"\D", "", raw or "")


def escape_like(s: str) -> str:
    """Escape user input for a literal LIKE needle (\\, %, _)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def name_tokens(needle: str) -> list[str]:
    """Split a name needle into AND-tokens, escaping LIKE wildcards.

    Whitespace-separated; every token must match (case-insensitive partial
    ILIKE %token%). Backslash/%/_ are escaped so user input is literal.
    Contact fallbacks (phone/email) escape the same way in search()."""
    tokens = []
    for tok in needle.strip().split():
        tok = escape_like(tok)
        if tok:
            tokens.append(tok)
    return tokens


def load_queries() -> dict[str, str]:
    """`-- name:` blocks from the shared file plus the app-only scoped variants.

    Two files on purpose: `customers_search.sql` is byte-mirrored against data,
    so the brand/group predicates that app serving needs (finding #75) live in
    `customers_search_scoped.sql`, which data does not have and must not police.
    """
    out: dict[str, str] = {}
    for path in (QUERIES_PATH, SCOPED_QUERIES_PATH):
        if not path.exists():
            continue
        parts = _NAME_RE.split(path.read_text(encoding="utf-8"))
        for i in range(1, len(parts) - 1, 2):
            name, sql = parts[i], parts[i + 1]
            lines = [ln for ln in sql.splitlines()
                     if not ln.strip().startswith("-- name:")]
            out[name] = "\n".join(lines).strip().rstrip(";")
    return out



def mask_phone(value: str) -> str:
    """+85291234567 -> +852 ****4567. Generic E.164: head + ' ****' + last4."""
    v = (value or "").strip()
    if len(v) <= 4:
        return "****" + v[-2:] if v else "****"
    return f"{v[:4]} ****{v[-4:]}"


def mask_email(value: str) -> str:
    """taiman.chan@example.com -> t****@example.com."""
    v = (value or "").strip()
    local, sep, domain = v.partition("@")
    if not sep or not domain:
        return "****"
    head = local[:1] if local else "*"
    return f"{head}****@{domain}"


def mask_contact(kind: str, value: str) -> str:
    if kind == "phone":
        return mask_phone(value)
    if kind == "email":
        return mask_email(value)
    return "****"


def mask_contacts(contacts: list[dict]) -> list[dict]:
    """Return copies with normalized+display PII masked (multi-match lists)."""
    out = []
    for c in contacts:
        masked = mask_contact(c.get("kind", ""), c.get("value_normalized", ""))
        row = dict(c)
        row["value_normalized"] = masked
        row["value_display"] = masked
        out.append(row)
    return out


async def _with_contacts(conn, customer_rows: list) -> list[dict]:
    """Attach contacts to customer rows; returns JSON-safe dicts."""
    items = [dict(r) for r in customer_rows]
    for it in items:
        it["id"] = str(it["id"])
        attrs = it.get("attributes")
        if not isinstance(attrs, dict):
            it["attributes"] = {}
    if not items:
        return items
    q = load_queries()
    ids = [UUID(it["id"]) for it in items]
    contact_rows = await conn.fetch(q["contacts_by_customer_ids"], ids)
    by_customer: dict[str, list[dict]] = {}
    for cr in contact_rows:
        row = dict(cr)
        row["id"] = str(row["id"])
        row["customer_id"] = str(row["customer_id"])
        by_customer.setdefault(row["customer_id"], []).append(row)
    for it in items:
        it["contacts"] = by_customer.get(it["id"], [])
    return items


async def _ids_for(conn, query: str, *args) -> set[str]:
    rows = await conn.fetch(query, *args)
    return {str(r["id"]) for r in rows}


async def search(conn, *, phone: str | None, email: str | None,
                 name: str | None, member_id: str | None,
                 group_id=None, brand_id=None,
                 member_partial: bool = True) -> tuple[list[dict], str]:
    """AND-intersect per-criterion matches. Returns (items, criteria_type).

    Name matching is multi-keyword AND: every whitespace-separated token must
    match (case-insensitive partial ILIKE %token%). Email matches exact
    normalized first, then a case-insensitive substring fallback (min
    EMAIL_PARTIAL_MIN_CHARS chars, LIKE-escaped); phone matches exact E.164
    first, then a digit-substring fallback (min PHONE_PARTIAL_MIN_DIGITS
    digits); member_id
    matches exact first, then falls back to
    a case-insensitive substring over member_id + external_member_id
    (low-sensitivity counter IDs, capped at MEMBER_PARTIAL_LIMIT) — unless
    member_partial=False, which keeps the GET direct-fetch path exact-only.

    group_id (a UUID, or None for group-wide sessions) silos results to one
    group; legacy ungrouped (NULL) rows stay visible to every session.

    brand_id (a UUID, or None for group-wide sessions) additionally requires
    the customer to belong to that brand, or to none yet: a completed order in
    the brand, OR no completed order anywhere (a fresh lead the store just
    created must still be findable). Identical to deps.require_customer_in_scope
    and to the list predicates in repos/customers.py, so a search hit and a
    by-id read agree. Fail closed: routers pass the session brand, never a
    request value.
    """
    from ..schemas.customer import normalize_email, normalize_phone

    q = load_queries()
    sets: list[set[str]] = []
    kinds: list[str] = []
    # Unnormalizable or too-short email/phone fragments match NOTHING
    # (200 empty) rather than erroring or returning candidates.
    norm_phone: str | None = None
    norm_email: str | None = None
    if phone is not None:
        digits = phone_digits(phone)
        try:
            norm_phone = normalize_phone(phone)
        except ValueError:
            norm_phone = None
        exact = await _ids_for(conn, q["customers_by_phone"], norm_phone) if norm_phone else set()
        if exact:
            sets.append(exact)
        elif len(digits) >= PHONE_PARTIAL_MIN_DIGITS:
            partial = await conn.fetch(q["customers_by_phone_partial_scoped"], digits,
                                        PHONE_PARTIAL_LIMIT, brand_id, group_id)
            sets.append({str(r["id"]) for r in partial})
        else:
            sets.append(set())
        kinds.append("phone")
    if email is not None:
        try:
            norm_email = normalize_email(email)
        except ValueError:
            norm_email = None
        exact = await _ids_for(conn, q["customers_by_email"], norm_email) if norm_email else set()
        needle = (email or "").strip().lower()
        if exact:
            sets.append(exact)
        elif len(needle) >= EMAIL_PARTIAL_MIN_CHARS:
            partial = await conn.fetch(
                q["customers_by_email_partial_scoped"],
                escape_like(needle), EMAIL_PARTIAL_LIMIT, brand_id, group_id,
            )
            sets.append({str(r["id"]) for r in partial})
        else:
            sets.append(set())
        kinds.append("email")
    if member_id is not None:
        needle = member_id.strip()
        exact = await _ids_for(conn, q["customers_by_member_id"], needle)
        if exact or not member_partial:
            sets.append(exact)
        else:
            partial = await conn.fetch(
                q["customers_by_member_id_partial_scoped"],
                escape_like(needle), MEMBER_PARTIAL_LIMIT, brand_id, group_id,
            )
            sets.append({str(r["id"]) for r in partial})
        kinds.append("member_id")
    if name is not None:
        tokens = name_tokens(name)
        rows = await conn.fetch(q["customers_by_name_tokens_scoped"], tokens, NAME_LIMIT,
                                brand_id, group_id)
        sets.append({str(r["id"]) for r in rows})
        kinds.append("name")
    matched = set.intersection(*sets) if sets else set()
    criteria_type = kinds[0] if len(kinds) == 1 else "mixed"
    if (group_id is not None or brand_id is not None) and matched:
        preds: list[str] = []
        args: list = [list(matched)]
        if group_id is not None:
            args.append(group_id)
            preds.append(f"(group_id = ${len(args)}::uuid OR group_id IS NULL)")
        if brand_id is not None:
            args.append(brand_id)
            preds.append(
                f"(EXISTS (SELECT 1 FROM orders o JOIN stores s ON s.id = o.store_id"
                f" WHERE o.customer_id = customers.id AND o.status = 'completed'"
                f" AND s.brand_id = ${len(args)}::uuid)"
                f" OR NOT EXISTS (SELECT 1 FROM orders o"
                f" WHERE o.customer_id = customers.id"
                f" AND o.status = 'completed'))"
            )
        scoped = await conn.fetch(
            "SELECT id FROM customers WHERE id = ANY($1::uuid[]) AND "
            + " AND ".join(preds),
            *args,
        )
        matched = {str(r["id"]) for r in scoped}
    if not matched:
        return [], criteria_type
    # Re-fetch full rows for matched ids (single query path per kind keeps SQL simple).
    if kinds == ["phone"]:
        rows = await _phone_rows(conn, q, norm_phone, phone_digits(phone),
                                 brand_id, group_id)
    elif kinds == ["email"]:
        rows = await _email_rows(conn, q, norm_email, (email or "").strip().lower(),
                                 brand_id, group_id)
    elif kinds == ["member_id"]:
        needle = (member_id or "").strip()
        rows = await conn.fetch(q["customers_by_member_id"], needle)
        if not rows and member_partial:
            rows = await conn.fetch(
                q["customers_by_member_id_partial_scoped"],
                escape_like(needle), MEMBER_PARTIAL_LIMIT, brand_id, group_id,
            )
    elif kinds == ["name"]:
        rows = await conn.fetch(
            q["customers_by_name_tokens_scoped"], name_tokens(name or ""), NAME_LIMIT,
            brand_id, group_id,
        )
    else:  # mixed: fetch by member/uuid-safe fallback via name-limited scan
        rows = await conn.fetch(
            q["customers_by_name_tokens_scoped"],
            name_tokens(name or "") if name else [],
            NAME_LIMIT, brand_id, group_id,
        )
        rows = [r for r in rows if str(r["id"]) in matched]
        if not rows and matched:
            # mixed without name: pull each single-criterion set's rows cheaply
            rows = []
            if phone is not None:
                rows += [r for r in await _phone_rows(conn, q, norm_phone, phone_digits(phone), brand_id, group_id) if str(r["id"]) in matched]
            if email is not None:
                rows += [r for r in await _email_rows(conn, q, norm_email, (email or "").strip().lower(), brand_id, group_id) if str(r["id"]) in matched]
            if member_id is not None:
                needle = member_id.strip()
                mrows = await conn.fetch(q["customers_by_member_id"], needle)
                if not mrows and member_partial:
                    mrows = await conn.fetch(
                        q["customers_by_member_id_partial_scoped"],
                        escape_like(needle), MEMBER_PARTIAL_LIMIT,
                        brand_id, group_id,
                    )
                rows += [r for r in mrows if str(r["id"]) in matched]
    rows = [r for r in rows if str(r["id"]) in matched]
    items = await _with_contacts(conn, rows)
    # Staff sees FULL contact values (owner override 2026-10-08). No masking.
    return items, criteria_type


async def _email_rows(conn, q: dict[str, str], norm_email: str | None,
                    needle: str, brand_id=None, group_id=None) -> list:
    """Exact normalized email rows first, case-insensitive substring
    fallback when the exact lookup is empty or the input never normalized.
    The needle is lowercased + LIKE-escaped by the caller.

    brand_id/group_id go into the capped fallback so its LIMIT applies after
    scoping (finding #75); the exact lookup has no cap and needs none.
    """
    rows = await conn.fetch(q["customers_by_email"], norm_email) if norm_email else []
    if not rows and len(needle) >= EMAIL_PARTIAL_MIN_CHARS:
        rows = await conn.fetch(
            q["customers_by_email_partial_scoped"],
            escape_like(needle), EMAIL_PARTIAL_LIMIT, brand_id, group_id,
        )
    return rows


async def _phone_rows(conn, q: dict[str, str], norm_phone: str | None,
                    digits: str, brand_id=None, group_id=None) -> list:
    """Exact normalized phone rows first, digit-substring fallback when the
exact lookup is empty or the input never normalized. Scope args go into the
capped fallback (finding #75)."""
    rows = await conn.fetch(q["customers_by_phone"], norm_phone) if norm_phone else []
    if not rows and len(digits) >= PHONE_PARTIAL_MIN_DIGITS:
        rows = await conn.fetch(q["customers_by_phone_partial_scoped"], digits,
                                PHONE_PARTIAL_LIMIT, brand_id, group_id)
    return rows


async def fetch_by_id(conn, customer_id: UUID) -> dict | None:
    q = load_queries()
    row = await conn.fetchrow(q["customer_by_id"], customer_id)
    if row is None:
        return None
    group = await conn.fetchval(
        "SELECT group_id FROM customers WHERE id = $1", customer_id
    )
    items = await _with_contacts(conn, [row])
    if not items:
        return None
    items[0]["group_id"] = str(group) if group is not None else None
    return items[0]


async def write_audit(conn, *, staff_id: str, customer_id: UUID | None,
                      criteria_type: str, result_count: int) -> None:
    """Append-only audit. criteria_type only — never raw phone/email values."""
    q = load_queries()
    await conn.execute(q["access_log_insert"], staff_id, customer_id, criteria_type, result_count)
