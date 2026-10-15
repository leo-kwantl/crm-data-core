"""LM1/LM2 model-result writers (data-owned tables, ml-owned engines).

Data lands the DDL first; ml's engines write rows matching this
grain. These helpers are the writer-side discipline: BTYD param keys
(r/alpha/a/b, all > 0) and assoc-eval metric rows, plus the
latest-fit read the serving layer joins scope-to-scope.
"""
from __future__ import annotations

from datetime import date
from typing import Any

BTYD_MODELS = ("bg-nbd", "pareto-nbd")
BTYD_PARAM_KEYS = ("r", "alpha", "a", "b")
ACTIVITIES = ("orders", "all")

BTYD_UPSERT_SQL = """
INSERT INTO btyd_params
  (as_of, activity, horizon_days, brand_id, group_id, model,
   params, n_customers, loglikelihood, fit_error)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10)
ON CONFLICT DO NOTHING
"""

# NOTE: no ON CONFLICT target — the grain UNIQUE is an expression
# index (NULL-safe COALESCE), which PG can't target directly; the
# writer resolves (as_of, activity, horizon, scope, model) first or
# tolerates the no-op via DO NOTHING.

LATEST_BTYD_PARAMS_SQL = """
SELECT DISTINCT ON (activity, horizon_days, brand_id, group_id, model)
       as_of, activity, horizon_days, brand_id, group_id, model,
       params, n_customers, loglikelihood, fit_error, fitted_at
FROM btyd_params
WHERE params IS NOT NULL
ORDER BY activity, horizon_days, brand_id, group_id, model, as_of DESC
"""

ASSOC_EVAL_UPSERT_SQL = """
INSERT INTO assoc_rules_eval
  (batch_id, eval_set, metric, value, n_holdout)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (batch_id, eval_set, metric) DO NOTHING
"""


def validate_btyd_params(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate an LM1 fit row. Returns a cleaned copy."""
    if not isinstance(spec, dict):
        raise ValueError("btyd fit must be a mapping")
    as_of = spec.get("as_of")
    if not isinstance(as_of, date):
        raise ValueError(f"as_of must be a date, got {as_of!r}")
    activity = spec.get("activity")
    if activity not in ACTIVITIES:
        raise ValueError(f"activity must be one of {ACTIVITIES}")
    horizon = spec.get("horizon_days")
    if (isinstance(horizon, bool) or not isinstance(horizon, int)
            or not 1 <= horizon <= 730):
        raise ValueError(
            f"horizon_days must be 1..730, got {horizon!r}")
    model = spec.get("model", "bg-nbd")
    if model not in BTYD_MODELS:
        raise ValueError(f"model must be one of {BTYD_MODELS}")
    params = spec.get("params")
    fit_error = spec.get("fit_error")
    if params is None and not fit_error:
        raise ValueError("a fit needs params or a fit_error (never neither)")
    if params is not None:
        if not isinstance(params, dict):
            raise ValueError("params must be a mapping")
        missing = [k for k in BTYD_PARAM_KEYS if k not in params]
        if missing:
            raise ValueError(
                f"params missing: {', '.join(missing)}")
        for key in BTYD_PARAM_KEYS:
            val = params[key]
            if (isinstance(val, bool) or not isinstance(val, (int, float))
                    or val <= 0):
                raise ValueError(
                    f"params[{key}] must be > 0, got {val!r}")
    n = spec.get("n_customers", 0)
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError(f"n_customers must be >= 0, got {n!r}")
    out = {"as_of": as_of, "activity": activity,
           "horizon_days": horizon, "model": model,
           "brand_id": spec.get("brand_id"),
           "group_id": spec.get("group_id"),
           "params": dict(params) if params is not None else None,
           "n_customers": n,
           "loglikelihood": spec.get("loglikelihood"),
           "fit_error": fit_error}
    return out


def validate_assoc_eval(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate an LM2 eval row. Returns a cleaned copy."""
    if not isinstance(spec, dict):
        raise ValueError("assoc eval must be a mapping")
    if not spec.get("batch_id"):
        raise ValueError("batch_id is required")
    eval_set = spec.get("eval_set", "holdout")
    if not isinstance(eval_set, str) or not eval_set.strip():
        raise ValueError("eval_set must be a non-empty string")
    metric = spec.get("metric")
    if not isinstance(metric, str) or not metric.strip():
        raise ValueError("metric must be a non-empty string")
    value = spec.get("value")
    if (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ValueError(f"value must be numeric, got {value!r}")
    n = spec.get("n_holdout", 0)
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError(f"n_holdout must be >= 0, got {n!r}")
    return {"batch_id": spec["batch_id"],
            "eval_set": eval_set.strip(), "metric": metric.strip(),
            "value": float(value), "n_holdout": n}
