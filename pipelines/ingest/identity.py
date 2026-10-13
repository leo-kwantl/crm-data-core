"""Identity ingest: external-ID binding + member-id resolution.

Contract (mirrors crm-cdp-app identity):
- `customers.member_id` format `^M-[0-9]{6}$`; `external_member_id` unique per source.
- Lookup order: `member_id` exact-first, then escaped ILIKE-substring over
  `member_id` + `external_member_id` (LIMIT 20). GET stays exact.
- Phone/email: exact match only (PII policy — never partial).
"""
from __future__ import annotations

import re

MEMBER_RE = re.compile(r"^M-[0-9]{6}$")


def normalize_binding(raw: dict) -> dict:
    """Validate one external-ID binding. Raises ValueError."""
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
