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
