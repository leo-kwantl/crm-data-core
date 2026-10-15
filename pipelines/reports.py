"""LD11 report-definition registry (data-owned, app-served).

A report definition pins (axes, metrics, filters) for one version of
one report page so snapshots and pages can't drift. Rows are
immutable: a new version is a new row, readers take MAX(version).
Any RFM/design writeup rides inside ``definition`` under the
``rfm_note`` key — deliberately no second table.
"""
from __future__ import annotations

from typing import Any

REQUIRED_KEYS = ("report_id", "name", "kind", "grain")
# A definition must declare at least one of these to be renderable.
CONTENT_KEYS = ("axes", "metrics", "filters")

REPORT_UPSERT_SQL = """
INSERT INTO report_definitions
  (report_id, version, name, kind, grain, definition)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
ON CONFLICT (report_id, version) DO NOTHING
"""

LATEST_DEFINITIONS_SQL = """
SELECT DISTINCT ON (report_id) report_id, version, name, kind, grain,
       definition, created_at
FROM report_definitions
ORDER BY report_id, version DESC
"""


def validate_report_definition(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate a report-definition spec. Returns a cleaned copy."""
    if not isinstance(spec, dict):
        raise ValueError("report definition must be a mapping")
    missing = [k for k in REQUIRED_KEYS if not spec.get(k)]
    if missing:
        raise ValueError(
            f"report definition missing: {', '.join(missing)}")
    for key in REQUIRED_KEYS:
        val = spec[key]
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"{key} must be a non-empty string")
    version = spec.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) \
            or version < 1:
        raise ValueError(f"version must be an int >= 1, got {version!r}")
    definition = spec.get("definition", {})
    if not isinstance(definition, dict):
        raise ValueError("definition must be a mapping")
    if not any(k in definition for k in CONTENT_KEYS):
        raise ValueError(
            f"definition needs at least one of {CONTENT_KEYS}")
    out = {k: spec[k].strip() for k in REQUIRED_KEYS}
    out["version"] = version
    out["definition"] = dict(definition)
    return out
