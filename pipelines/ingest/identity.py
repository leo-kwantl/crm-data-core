"""Identity ingest: external-ID binding + member-id resolution.

Contract (mirrors crm-cdp-app identity):
- `customers.member_id` format `^M-[0-9]{6}$`; `external_member_id` unique per source.
- Lookup order: `member_id` exact-first, then escaped ILIKE-substring over
  `member_id` + `external_member_id` (LIMIT 20). GET stays exact.
- Phone/email: exact-first; a POST search may fall back to a bounded substring
  (min 3 digits / 3 chars, LIMIT 20, criteria-type-only audit). GET stays exact.
"""
from __future__ import annotations

import re
from typing import TypedDict

MEMBER_RE = re.compile(r"^M-[0-9]{6}$")


class IdentityPayload(TypedDict):
    """The shape `normalize_binding` hands `apply._apply_identity`.

    `source` is validated here and not written by the binding UPDATE: it is
    the provenance of the claim, which `customer_contacts`/audit carry, and
    the shape test allowlists it as deliberately unconsumed.
    """

    member_id: str
    external_member_id: str
    source: str


def normalize_binding(raw: dict, exclusions: list = ()) -> IdentityPayload:
    """Validate one external-ID binding. Raises ValueError."""
    from pipelines.exclusions import check_excluded
    check_excluded(exclusions, "customer", raw.get("member_id"))
    member_id = raw.get("member_id", "")
    if not MEMBER_RE.match(member_id):
        raise ValueError(f"bad member_id (want M-NNNNNN): {member_id!r}")
    if not raw.get("external_member_id") or not raw.get("source"):
        raise ValueError("external_member_id + source required")
    return {
        "member_id": member_id,
        "external_member_id": raw["external_member_id"],
        "source": raw["source"],
    }
