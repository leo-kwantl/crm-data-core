"""Source connector framework: registry + validate-on-create + sanitize-on-read.

Contract (see pipelines/contracts.md "019: source connector framework"):
- source_types are equals: shopify | csv | excel | delta_lake | rest_api
  (+ woocommerce RESERVED — registration rejected until its spec lands).
- validate-on-create: connector configs are validated at registration
  (required per-type keys, no embedded secrets); bad configs never reach
  a sync run.
- sanitize-on-read: logs never carry PII or secrets; quarantine `raw`
  keeps PII (exact-match reprocessing) but secrets are redacted at write.
- secrets-at-rest: `connectors.config` holds NON-SECRET params only.
  Credentials live in env/secret-manager; `secret_ref` names the env var.
  The DB never stores a plaintext secret. Rotation = update env, no DDL.

Stages stay: extract (per-type client, outside this module) -> normalize
(pipelines/ingest/{orders,events,identity}) -> load (validated upsert).
This module is the registry/validation layer in front of extract.
"""
from __future__ import annotations

from typing import Any

SOURCE_TYPES = ("shopify", "csv", "excel", "delta_lake", "rest_api")
RESERVED_TYPES = ("woocommerce",)

# Per-type required config keys (validate-on-create). Values are
# non-secret connection params; anything credential-shaped is rejected.
REQUIRED_CONFIG: dict[str, frozenset[str]] = {
    "shopify": frozenset({"shop_domain"}),
    "csv": frozenset({"column_map"}),
    "excel": frozenset({"column_map"}),
    "delta_lake": frozenset({"table"}),
    "rest_api": frozenset({"base_url"}),
}

# Key suffixes/names treated as credential-shaped (never in config,
# redacted by sanitize_for_log).
SECRET_MARKERS = ("secret", "token", "password", "api_key", "apikey",
                  "authorization", "cookie", "private_key")


def _is_secret_key(key: str) -> bool:
    k = key.lower().replace("-", "_")
    return any(m in k for m in SECRET_MARKERS) or k.endswith("_key")


# PII keys redacted from logs (kept in quarantine raw for reprocessing).
PII_KEYS = frozenset({"phone", "email", "address_line", "date_of_birth"})

REDACTED = "***"


def validate_connector(source_type: str, name: str,
                       config: dict[str, Any] | None,
                       secret_ref: str | None = None) -> dict[str, Any]:
    """Validate a connector registration. Returns normalized record.

    Raises ValueError on: unknown source_type, reserved type
    (woocommerce), blank name, missing required config keys, embedded
    secret values in config, or a secret_ref that looks like a value
    rather than an env-var name.
    """
    if source_type in RESERVED_TYPES:
        raise ValueError(
            f"source_type '{source_type}' is reserved "
            "(spec pending) — registration rejected")
    if source_type not in SOURCE_TYPES:
        raise ValueError(
            f"unknown source_type '{source_type}' "
            f"(expected one of {', '.join(SOURCE_TYPES)})")
    if not (name or "").strip():
        raise ValueError("connector name is required")
    cfg = dict(config or {})
    if "pagination" in cfg:
        cfg["pagination"] = validate_pagination(cfg["pagination"])
    missing = [k for k in REQUIRED_CONFIG[source_type] if k not in cfg]
    if missing:
        raise ValueError(
            f"source_type '{source_type}' missing required config keys: "
            f"{', '.join(sorted(missing))}")
    embedded = [k for k in cfg if _is_secret_key(k)]
    if embedded:
        raise ValueError(
            "config must not embed secrets "
            f"(move to secret manager, reference via secret_ref): "
            f"{', '.join(sorted(embedded))}")
    if secret_ref is not None:
        ref = secret_ref.strip()
        if not ref:
            raise ValueError("secret_ref must be an env-var name, not blank")
        if any(ch.isspace() for ch in ref) or len(ref) > 128:
            raise ValueError(
                "secret_ref must be an env-var name (no spaces, <=128ch); "
                "the DB never stores the secret itself")
    return {"source_type": source_type, "name": name.strip(),
            "config": cfg, "secret_ref": secret_ref}


# --- LD2: per-connector schedule + incremental-extract semantics ---
#
# schedule_cron: NULL/empty = manual-only; else validated 5-field cron.
# pagination: optional `pagination` block in connector config, validated
# at registration. Cursor pagination is MANDATORY vocabulary (page-only
# extracts are rejected as a config smell): every paginated source must
# declare how cursors work, even when it pages today.
# since pushdown: server-side `since` param first (shopify/rest_api),
# client-side since_filter fallback (csv/excel/delta_lake file scans).
# since fail-open: rows with missing/unparseable dates are INCLUDED and
# flagged for quarantine review — incremental extract must never
# silently drop rows it cannot date.

PAGINATION_TYPES = ("page", "cursor", "offset")

_CRON_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def validate_schedule_cron(expr: str | None) -> str | None:
    """Validate a 5-field cron (NULL/blank = manual-only)."""
    if expr is None or not str(expr).strip():
        return None
    parts = str(expr).strip().split()
    if len(parts) != 5:
        raise ValueError(
            f"schedule_cron must have 5 fields, got {len(parts)}:"
            f" {expr!r}")
    for part, (lo, hi) in zip(parts, _CRON_BOUNDS):
        body = part[2:] if part.startswith("*/") else part
        if body == "*":
            continue
        try:
            vals = [int(v) for v in body.split(",")]
        except ValueError:
            raise ValueError(
                f"bad schedule_cron field {part!r} in {expr!r}"
            ) from None
        if not vals or any(v < lo or v > hi for v in vals):
            raise ValueError(
                f"schedule_cron field {part!r} out of range in {expr!r}")
    return " ".join(parts)


def validate_pagination(spec: dict[str, Any] | None) -> dict | None:
    """Validate a `pagination` config block. None -> None (unpaid)."""
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise ValueError("pagination must be a mapping")
    ptype = spec.get("type", "page")
    if ptype not in PAGINATION_TYPES:
        raise ValueError(
            f"pagination type {ptype!r} unknown "
            f"(expected one of {PAGINATION_TYPES})")
    out = dict(spec)
    if ptype == "cursor":
        if not spec.get("cursor_param"):
            raise ValueError(
                "cursor pagination needs 'cursor_param' "
                "(request field carrying the cursor)")
        out.setdefault("response_key", "next_cursor")
    elif ptype == "page":
        out.setdefault("page_param", "page")
        out.setdefault("limit_param", "limit")
    else:  # offset
        out.setdefault("offset_param", "offset")
        out.setdefault("limit_param", "limit")
    limit = spec.get("default_limit", 100)
    if not isinstance(limit, int) or limit < 1 or limit > 1000:
        raise ValueError(
            f"pagination default_limit must be 1..1000, got {limit!r}")
    out["default_limit"] = limit
    return out


# Source types whose extract accepts a server-side `since` bound
# (query param / API filter) vs file/table scans filtered client-side.
SERVER_SINCE_TYPES = frozenset({"shopify", "rest_api"})


def since_pushdown(source_type: str) -> str:
    """Where does the `since` bound apply: 'server' or 'client'."""
    if source_type in SOURCE_TYPES:
        return ("server" if source_type in SERVER_SINCE_TYPES
                else "client")
    raise ValueError(f"unknown source_type: {source_type!r}")


def _parse_date(value: Any):
    from datetime import datetime

    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(
                value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def since_filter(records: list[dict], since, field: str = "updated_at"
                 ) -> tuple[list[dict], list[dict]]:
    """Client-side incremental filter with fail-open dating.

    Returns (kept, flagged): kept = rows dated >= since PLUS rows
    with missing/unparseable dates (never silently dropped); flagged
    = [{"index", "reason"}] for quarantine review.
    """
    from datetime import datetime

    if isinstance(since, str):
        since_dt = _parse_date(since)
        if since_dt is None:
            raise ValueError(f"unparseable since bound: {since!r}")
    elif isinstance(since, datetime):
        since_dt = since
    else:
        raise ValueError(
            f"since must be datetime or ISO string, got {type(since)}")
    kept: list[dict] = []
    flagged: list[dict] = []
    for i, rec in enumerate(records):
        dt = _parse_date(rec.get(field))
        if dt is None:
            kept.append(rec)  # fail-open: include + flag
            flagged.append({"index": i,
                            "reason": f"undateable {field!r}; "
                            "included by fail-open rule"})
        elif dt >= since_dt:
            kept.append(rec)
    return kept, flagged


def should_retry(status_or_exc: Any) -> bool:
    """Retry extract on 429 / 5xx / transport errors; never on 4xx."""
    if isinstance(status_or_exc, int):
        return status_or_exc == 429 or 500 <= status_or_exc <= 599
    name = type(status_or_exc).__name__
    return name in ("TimeoutError", "ConnectionError",
                     "ConnectTimeout", "ReadTimeout",
                     "httpx.TimeoutException",
                     "httpx.ConnectError")


def compute_backoff(attempt: int, base_s: float = 1.0,
                    cap_s: float = 60.0) -> float:
    """Exponential backoff: min(cap, base * 2**attempt)."""
    if not isinstance(attempt, int) or attempt < 0:
        raise ValueError(f"attempt must be >= 0, got {attempt!r}")
    if base_s <= 0 or cap_s <= 0:
        raise ValueError("base_s and cap_s must be positive")
    return min(cap_s, base_s * (2 ** attempt))


def sync_log(sync_run_id: str, event: str, **fields: Any) -> str:
    """One structured log line, correlated by sync run id (LD2-e)."""
    import json

    return json.dumps({"sync_run_id": str(sync_run_id), "event": event,
                       **{k: str(v) for k, v in fields.items()}})


def describe_extract(records: list[dict], limit: int = 5) -> dict:
    """Shape probe for connection checks: columns + sample row count."""
    cols: set[str] = set()
    for rec in records:
        cols.update(str(k) for k in rec)
    return {"columns": sorted(cols),
            "sample_rows": min(len(records), limit),
            "sample": records[:limit]}


def sanitize_for_log(obj: Any) -> Any:
    """Deep-copy `obj` with PII + secret values replaced by '***'.

    For log lines, error messages, and API surfaces. Quarantine `raw`
    is NOT routed through this (PII kept for exact-match reprocessing;
    secrets redacted at the quarantine writer instead).
    """
    if isinstance(obj, dict):
        return {k: (REDACTED if (_is_secret_key(str(k))
                                 or str(k).lower() in PII_KEYS)
                    else sanitize_for_log(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_log(v) for v in obj]
    return obj


def redact_secrets(obj: Any) -> Any:
    """Deep-copy `obj` with secret values replaced by '***' (PII kept).

    Used by the quarantine writer: rejected-row `raw` keeps PII so rows
    can be reprocessed by exact match, but never persists a secret.
    """
    if isinstance(obj, dict):
        return {k: (REDACTED if _is_secret_key(str(k))
                    else redact_secrets(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_secrets(v) for v in obj]
    return obj
