"""Search repository: pure SQL over asyncpg. Canonical SQL lives in
db/queries/customers_search.sql (parsed by -- name: markers).

PII rules:
- phone/email match EXACT normalized values only (E.164 / lowercase).
- Partial phone/email input never matches (normalization rejects it).
- Contacts are returned in FULL for staff (owner override 2026-10-08:
  staff MUST see full values in list + record UI). mask_* helpers are
  retained for potential future use + tests, but serialization never masks.
- Audit rows carry criteria_type + result_count, NEVER raw phone/email values.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

QUERIES_PATH = Path(__file__).resolve().parents[3] / "db" / "queries" / "customers_search.sql"

_NAME_RE = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.M)
NAME_LIMIT = 20
MEMBER_PARTIAL_LIMIT = 20


def escape_like(s: str) -> str:
    """Escape user input for a literal LIKE needle (\\, %, _)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def name_tokens(needle: str) -> list[str]:
    """Split a name needle into AND-tokens, escaping LIKE wildcards.

    Whitespace-separated; every token must match (case-insensitive partial
    ILIKE %token%). Backslash/%/_ are escaped so user input is literal.
    Phone/email matching is untouched (exact normalized only, elsewhere)."""
    tokens = []
    for tok in needle.strip().split():
        tok = escape_like(tok)
        if tok:
            tokens.append(tok)
    return tokens


def load_queries() -> dict[str, str]:
    text = QUERIES_PATH.read_text(encoding="utf-8")
    parts = _NAME_RE.split(text)
    out: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        name, sql = parts[i], parts[i + 1]
        lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("-- name:")]
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
                 group_id=None, member_partial: bool = True) -> tuple[list[dict], str]:
    """AND-intersect per-criterion matches. Returns (items, criteria_type).

    Name matching is multi-keyword AND: every whitespace-separated token must
    match (case-insensitive partial ILIKE %token%). Phone/email stay
    exact-normalized-only; member_id matches exact first, then falls back to
    a case-insensitive substring over member_id + external_member_id
    (low-sensitivity counter IDs, capped at MEMBER_PARTIAL_LIMIT) — unless
    member_partial=False, which keeps the GET direct-fetch path exact-only.

    group_id (a UUID, or None for group-wide sessions) silos results to one
    group; legacy ungrouped (NULL) rows stay visible to every session."""
    from ..schemas.customer import normalize_email, normalize_phone

    q = load_queries()
    sets: list[set[str]] = []
    kinds: list[str] = []
    # Unnormalizable input (e.g. last-4-digits partial phone) matches NOTHING
    # (200 empty) rather than erroring or returning candidates: exact-only.
    norm_phone: str | None = None
    norm_email: str | None = None
    if phone is not None:
        try:
            norm_phone = normalize_phone(phone)
        except ValueError:
            norm_phone = None
            sets.append(set())
        else:
            sets.append(await _ids_for(conn, q["customers_by_phone"], norm_phone))
        kinds.append("phone")
    if email is not None:
        try:
            norm_email = normalize_email(email)
        except ValueError:
            norm_email = None
            sets.append(set())
        else:
            sets.append(await _ids_for(conn, q["customers_by_email"], norm_email))
        kinds.append("email")
    if member_id is not None:
        needle = member_id.strip()
        exact = await _ids_for(conn, q["customers_by_member_id"], needle)
        if exact or not member_partial:
            sets.append(exact)
        else:
            partial = await conn.fetch(
                q["customers_by_member_id_partial"],
                escape_like(needle), MEMBER_PARTIAL_LIMIT,
            )
            sets.append({str(r["id"]) for r in partial})
        kinds.append("member_id")
    if name is not None:
        tokens = name_tokens(name)
        rows = await conn.fetch(q["customers_by_name_tokens"], tokens, NAME_LIMIT)
        sets.append({str(r["id"]) for r in rows})
        kinds.append("name")
    matched = set.intersection(*sets) if sets else set()
    criteria_type = kinds[0] if len(kinds) == 1 else "mixed"
    if group_id is not None and matched:
        scoped = await conn.fetch(
            "SELECT id FROM customers WHERE id = ANY($1::uuid[])"
            " AND (group_id = $2::uuid OR group_id IS NULL)",
            list(matched),
            group_id,
        )
        matched = {str(r["id"]) for r in scoped}
    if not matched:
        return [], criteria_type
    # Re-fetch full rows for matched ids (single query path per kind keeps SQL simple).
    if kinds == ["phone"]:
        rows = await conn.fetch(q["customers_by_phone"], norm_phone) if norm_phone else []
    elif kinds == ["email"]:
        rows = await conn.fetch(q["customers_by_email"], norm_email) if norm_email else []
    elif kinds == ["member_id"]:
        needle = (member_id or "").strip()
        rows = await conn.fetch(q["customers_by_member_id"], needle)
        if not rows and member_partial:
            rows = await conn.fetch(
                q["customers_by_member_id_partial"],
                escape_like(needle), MEMBER_PARTIAL_LIMIT,
            )
    elif kinds == ["name"]:
        rows = await conn.fetch(
            q["customers_by_name_tokens"], name_tokens(name or ""), NAME_LIMIT
        )
    else:  # mixed: fetch by member/uuid-safe fallback via name-limited scan
        rows = await conn.fetch(
            q["customers_by_name_tokens"],
            name_tokens(name or "") if name else [],
            NAME_LIMIT,
        )
        rows = [r for r in rows if str(r["id"]) in matched]
        if not rows and matched:
            # mixed without name: pull each single-criterion set's rows cheaply
            rows = []
            if norm_phone is not None:
                rows += [r for r in await conn.fetch(q["customers_by_phone"], norm_phone) if str(r["id"]) in matched]
            if norm_email is not None:
                rows += [r for r in await conn.fetch(q["customers_by_email"], norm_email) if str(r["id"]) in matched]
            if member_id is not None:
                needle = member_id.strip()
                mrows = await conn.fetch(q["customers_by_member_id"], needle)
                if not mrows and member_partial:
                    mrows = await conn.fetch(
                        q["customers_by_member_id_partial"],
                        escape_like(needle), MEMBER_PARTIAL_LIMIT,
                    )
                rows += [r for r in mrows if str(r["id"]) in matched]
    rows = [r for r in rows if str(r["id"]) in matched]
    items = await _with_contacts(conn, rows)
    # Staff sees FULL contact values (owner override 2026-10-08). No masking.
    return items, criteria_type


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
