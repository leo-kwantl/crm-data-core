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

    Only the two values the binding UPDATE writes: `source` is validated in
    `normalize_binding` and then dropped, because no store holds it.
    """

    member_id: str
    external_member_id: str


def normalize_binding(raw: dict, exclusions: list = ()) -> IdentityPayload:
    """Validate one external-ID binding. Raises ValueError."""
    from pipelines.exclusions import check_excluded
    check_excluded(exclusions, "customer", raw.get("member_id"))
    member_id = raw.get("member_id", "")
    if not MEMBER_RE.match(member_id):
        raise ValueError(f"bad member_id (want M-NNNNNN): {member_id!r}")
    if not raw.get("external_member_id") or not raw.get("source"):
        raise ValueError("external_member_id + source required")
    # `source` is required on the wire (provenance of the binding claim) but is
    # not carried: the binding UPDATE writes two values, and no store holds it,
    # so carrying it would make the payload claim something nothing reads.
    return {
        "member_id": member_id,
        "external_member_id": raw["external_member_id"],
    }
