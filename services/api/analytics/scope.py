"""Scope helpers for analytics (single source of truth for row visibility).

A session's scope is the (brand, group) tuple from session_scope():
- a staff row that carries a BRAND: (brand_id, None) -- the BRAND's scope.
- a staff row that carries a GROUP but no brand: NO analytics scope at all
  (the group is a customer attribute, not a scope -- see below); every
  analytics read answers 403 {"code": "no_analytics_scope"} instead of a
  zero-filled page that reads as "no activity".
- a staff row that carries NEITHER: (None, None) = global.

THE GROUP IS NOT A SCOPE (A114, manager, 2026-10-22)
---------------------------------------------------
The group stopped being a scope on 2026-10-22, and the code says why:

- No analytics row has ever carried a group: ml's census found 0 group-alone
  rows in every scope-shaped table (0 of 20 forecast, 12 assoc, 39 explain,
  34 churn, 19 clv, 51 rfm, 45 segment) and 0 group rows ever served.
- The shape session_scope used to promise -- (None, group_id) -- is a scope
  NO writer can produce: the writers' predicate is brand-scoped (the brand's
  group AND the customer's group), so the only writable group scope would be
  the PAIR (brand_id, group_id).
- The population behind a group leg was never settled.

So the scope a staff member gets comes from THE STAFF ROW'S OWN SHAPE: the
brand it is linked to (its authority is the brand), the global scope when it
is linked to neither, and NO scope at all when it carries a group without a
brand. The role is NOT part of the rule any more: a manager linked to a
brand reads that brand, exactly like an sa linked to the same brand.

The group stays a CUSTOMER ATTRIBUTE: customers.group_id is read, filtered
and returned like any other customer field, and nothing in this module
removes it.

Snapshot/batch rows are stamped with exactly that tuple at write time.
Visibility is stamp-equality (NULL-safe on both sides):

    visible <=> row.brand == session.brand AND row.group == session.group

Consequences (deliberate, quarantine-by-default):
- legacy pre-scope rows (NULL/NULL) are HQ-only -- no brand session can
  list or open rows computed over all brands before scoping existed.
- admin-created global batches (NULL/NULL) are likewise HQ-only; brand
  sessions see only their own brand's runs.
- a brand session reads brand-stamped runs only; the group's aggregate is
  not a scope any session holds, so no session reads a group-stamped row.
- the equality is mutual: a brand-less HQ session (None, None) matches only
  NULL/NULL rows, so brand-stamped runs are invisible to HQ by
  construction. Read a brand's runs from a brand-scoped session; do not relax
  either SQL predicate to the population form to "let admin see brand runs" --
  a brand session would then also match global/legacy rows.

To serve "this session's rows or the global ones", the population form cannot
be used (with (B, None) it reduces to `brand_id = B`); write the disjunction
instead: `(brand_id IS NULL AND group_id IS NULL) OR (brand_id IS NOT DISTINCT
FROM $n AND group_id IS NOT DISTINCT FROM $m)`.

A row stamped with BOTH a brand and a group is read by NOBODY: the brand
session reads `group_id IS NULL`, a group session does not exist any more,
and HQ reads both NULL. That is an under-read, never a leak, but it is a
silent one - the brand's page says "no snapshot" while the rows exist. A
writer must stamp exactly ONE of the two (a brand row: brand set, group
NULL; a group row: brand NULL, group set) or neither (the global batch), and
must never stamp both.

The SQL list filters use the identical predicate
(brand_id IS NOT DISTINCT FROM $brand AND group_id IS NOT DISTINCT FROM
$group); keep the two in sync -- row_visible is the reference implementation.

Classify each predicate by the row being located: the located row takes
stamp-equality on its scope stamp, a joined source row takes the
parameter-guarded population form ($n IS NULL OR col = $n). One surface may
carry both -- e.g. db/queries/analytics_snapshots.sql:110-111 (live rows) vs
its stamped reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

_LEGACY_NOTE = "legacy pre-scope rows are HQ-only"

#: The machine code a session with NO analytics scope is refused with. It is
#: a code of its own (not `forbidden`): the session is not forbidden, it has
#: nothing to read, and a page has to say so instead of rendering an empty
#: page that reads as "no activity".
NO_ANALYTICS_SCOPE = "no_analytics_scope"

#: WHY a session has no scope: its staff row carries a group and no brand.
#: The group is a customer attribute (A114), not a scope, so there is nothing
#: to read -- not the group, and not the global scope either.
NO_SCOPE_REASON = "group_without_brand"


@dataclass(frozen=True)
class SessionScope:
    """The scope one session reads, in the app's own words.

    `kind` is the word a page can render without inferring anything:
    "brand" (the staff row's brand), "global" (the staff row carries neither
    a brand nor a group) or "none" (the staff row carries a group and no
    brand -- no analytics scope at all).

    The group element is always None: the group is not a scope (A114), so a
    session never carries one and a query never filters on it.
    """

    kind: str
    brand_id: Any = None
    group_id: Any = None

    @property
    def filters(self) -> tuple:
        """(brand filter, group filter) for the SQL scope predicates."""
        return (self.brand_id, self.group_id)

    @property
    def has_scope(self) -> bool:
        """False for the one state that has no scope at all."""
        return self.kind != "none"


def session_scope(user) -> SessionScope:
    """The scope this session reads, decided by the STAFF ROW's own shape.

    Duck-typed on user: needs .brand_id/.group_id only, so repos and routers
    share it. The role is deliberately NOT read: the staff row's shape is the
    rule (A114), so a manager linked to a brand reads that brand.
    """
    brand = getattr(user, "brand_id", None)
    group = getattr(user, "group_id", None)
    if brand is not None:
        return SessionScope("brand", brand, None)
    if group is not None:
        # A group without a brand: the group is a customer attribute, so it
        # is not a scope, and the global scope is NOT a consolation prize
        # for a member who has no scope of their own.
        return SessionScope("none")
    return SessionScope("global", None, None)


def no_scope_error() -> HTTPException:
    """The 403 a session with no analytics scope is refused with."""
    return HTTPException(status_code=403, detail={
        "code": NO_ANALYTICS_SCOPE,
        "reason": NO_SCOPE_REASON,
    })


def _key(v) -> str | None:
    return str(v) if v is not None else None


def _visible(scope: SessionScope, row: dict) -> bool:
    return (_key(row.get("brand_id")) == _key(scope.brand_id)
            and _key(row.get("group_id")) == _key(scope.group_id))


def row_visible(user, row: dict) -> bool:
    """True when the stamped row sits inside the caller's session scope."""
    scope = session_scope(user)
    if not scope.has_scope:
        return False
    return _visible(scope, row)


def guard_scoped_row(user, row: dict, *, not_found_code: str) -> None:
    """404 when a snapshot/batch row sits outside the caller's scope.

    A session with NO scope at all is refused out loud (403
    no_analytics_scope): "no snapshot" would read as "no activity", which is
    a different fact.
    """
    scope = session_scope(user)
    if not scope.has_scope:
        raise no_scope_error()
    if not _visible(scope, row):
        raise HTTPException(status_code=404,
                            detail={"code": not_found_code})


def stamp_scope(row: dict) -> dict:
    """The scope a served row was stamped with, in reader words.

    The row's OWN stamp, never the session's: a reader has to be able to
    see whose plan they are reading, and the HQ session is exactly the case
    where the two differ (HQ reads across scopes; a brand session reads its
    own). `kind` is the word the reader needs -- "global", "brand" or
    "group" -- so a page never has to infer the scope from a pair of ids.

    A batch stamped with a brand is NOT the company plan: the global reads
    filter to `brand_id IS NULL AND group_id IS NULL`, and this helper
    reports what was actually served, so a payload can name it.

    "group" is a STAMP, not a scope (A114): no session holds a group scope,
    so a group-stamped row is read by nobody. The word stays because it
    describes the row truthfully -- it is how an under-read is told apart
    from a missing row.
    """
    brand = row.get("brand_id")
    group = row.get("group_id")
    if brand is None and group is None:
        kind = "global"
    elif group is None:
        kind = "brand"
    else:
        kind = "group"
    return {"kind": kind, "brand_id": _key(brand), "group_id": _key(group)}
