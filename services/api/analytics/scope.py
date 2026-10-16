"""Scope helpers for analytics (single source of truth for row visibility).

A session's scope is the (brand, group) tuple from session_scope():
- brand-scoped staff (role sa with a brand link): (brand_id, None).
- group-wide roles (manager/admin) with a session group: (None, group_id).
- brand-less HQ sessions: (None, None) = global.

Snapshot/batch rows are stamped with exactly that tuple at write time.
Visibility is stamp-equality (NULL-safe on both sides):

    visible <=> row.brand == session.brand AND row.group == session.group

Consequences (deliberate, quarantine-by-default):
- legacy pre-scope rows (NULL/NULL) are HQ-only — no group session can
  list or open rows computed over all brands before scoping existed.
- admin-created global batches (NULL/NULL) are likewise HQ-only; brand
  sessions see only their own brand's runs.
- a group-wide manager sees group-stamped runs but not individual brand
  rows; brand detail needs a brand-scoped session. Live compute endpoints
  (RFM/churn/CLV/segments) still aggregate the whole group for managers.
- the equality is mutual, so a brand-less HQ session (None, None) matches
  only NULL/NULL rows: brand- and group-stamped runs are invisible to HQ by
  construction, the same comparison read the other way. Read a brand's runs
  from a brand-scoped session; do not relax either SQL predicate to the
  population form to "let admin see brand runs" — that trades a blindness
  bug for a disclosure bug, since a brand session would then also match
  global/legacy rows.

If a product case ever needs "this session's rows or the global ones", the
population form cannot express it: with a brand session (B, None) it reduces
to `brand_id = B` and excludes the global rows it was meant to add. Write the
disjunction out instead — global rows by their explicit tuple, session rows by
stamp-equality: `(brand_id IS NULL AND group_id IS NULL) OR (brand_id IS NOT
DISTINCT FROM $n AND group_id IS NOT DISTINCT FROM $m)`. The tuple form is
also what keeps a group-stamped (NULL, G) row out of a brand session, and
(NULL, G) is the normal shape: session_scope returns (brand, None) or
(None, group).

The SQL list filters use the identical predicate
(brand_id IS NOT DISTINCT FROM $brand AND group_id IS NOT DISTINCT FROM
$group); keep the two in sync — row_visible is the reference implementation.

Classify each predicate by the row being located, not by the column or the
table: the located row takes stamp-equality on its scope stamp (NULL-safe on
both sides), a joined source row takes the parameter-guarded population form
($n IS NULL OR col = $n) — the same column name is a stamp on one and a plain
dimension on the other. One stamped surface may carry both — a snapshot list
filtered by mode, or a join to live brands/groups (see
db/queries/analytics_snapshots.sql:110-111, whose stamped reads are
stamp-equality at :20-23/:40-43/:62-65).
"""

from __future__ import annotations

from fastapi import HTTPException

_LEGACY_NOTE = "legacy pre-scope rows are HQ-only"


def session_scope(user) -> tuple:
    """(brand filter, group filter) for the session. Duck-typed on user:
    needs .role/.brand_id/.group_id only, so repos and routers share it."""
    if user.role in ("admin", "manager"):
        return None, user.group_id
    return user.brand_id, None


def _key(v) -> str | None:
    return str(v) if v is not None else None


def row_visible(user, row: dict) -> bool:
    """True when the stamped row sits inside the caller's session scope."""
    b, g = session_scope(user)
    return (_key(row.get("brand_id")) == _key(b)
            and _key(row.get("group_id")) == _key(g))


def guard_scoped_row(user, row: dict, *, not_found_code: str) -> None:
    """404 when a snapshot/batch row sits outside the caller's scope."""
    if not row_visible(user, row):
        raise HTTPException(status_code=404,
                            detail={"code": not_found_code})
