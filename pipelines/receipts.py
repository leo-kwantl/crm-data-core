"""LD12 landing receipts (data-owned, immutable history).

One receipt per landed batch — either a file upload (``upload_job_id``)
or a vendor batch import (``vendor_batch_id``), never both, never
neither. Counts must reconcile (loaded + quarantined <= seen); the
receipt is the batch's proof of landing, not a second counter.
``manifest_pipeline`` links the scheduler manifest that drove the load
(``pipeline_manifests`` PK); NULL for manual/ad-hoc loads.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

RECEIPT_INSERT_SQL = """
INSERT INTO landing_receipts
  (upload_job_id, vendor_batch_id, source, rows_seen, rows_loaded,
   rows_quarantined, contract, manifest_pipeline)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING id, received_at
"""

RECEIPTS_FOR_JOB_SQL = """
SELECT id, vendor_batch_id, source, rows_seen, rows_loaded,
       rows_quarantined, contract, manifest_pipeline, received_at
FROM landing_receipts
WHERE upload_job_id = $1
"""


def _as_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError):
        raise ValueError(
            f"upload_job_id must be a UUID, got {value!r}") from None


def validate_receipt(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate a landing-receipt spec. Returns a cleaned copy."""
    if not isinstance(spec, dict):
        raise ValueError("landing receipt must be a mapping")
    job = _as_uuid(spec.get("upload_job_id"))
    vendor = spec.get("vendor_batch_id")
    if vendor is not None:
        if not isinstance(vendor, str) or not vendor.strip():
            raise ValueError("vendor_batch_id must be a non-empty string")
        vendor = vendor.strip()
    if (job is None) == (vendor is None):
        raise ValueError(
            "exactly one of upload_job_id / vendor_batch_id is required")
    source = spec.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")
    counts: dict[str, int] = {}
    for key in ("rows_seen", "rows_loaded", "rows_quarantined"):
        val = spec.get(key, 0)
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise ValueError(f"{key} must be an int >= 0, got {val!r}")
        counts[key] = val
    if counts["rows_loaded"] + counts["rows_quarantined"] \
            > counts["rows_seen"]:
        raise ValueError(
            f"loaded ({counts['rows_loaded']}) + quarantined "
            f"({counts['rows_quarantined']}) exceeds seen "
            f"({counts['rows_seen']})")
    contract = spec.get("contract")
    if not isinstance(contract, str) or not contract.strip():
        raise ValueError("contract must be a non-empty string")
    manifest = spec.get("manifest_pipeline")
    if manifest is not None:
        if not isinstance(manifest, str) or not manifest.strip():
            raise ValueError(
                "manifest_pipeline must be a non-empty string or null")
        manifest = manifest.strip()
    return {"upload_job_id": job, "vendor_batch_id": vendor,
            "source": source.strip(), **counts,
            "contract": contract.strip(),
            "manifest_pipeline": manifest}
