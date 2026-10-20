"""LM1/LM2 model-result writers (data-owned tables, ml-owned engines).

Data lands the DDL first; ml's engines write rows matching this
grain. These helpers are the writer-side discipline: BTYD param keys
(r/alpha/a/b, all > 0) and LM2 comparison-matrix rows, plus the
latest-fit read the serving layer joins scope-to-scope.
"""
from __future__ import annotations

from datetime import date
from typing import Any

BTYD_MODELS = ("bg_nbd", "pareto_nbd")
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

MATRIX_UPSERT_SQL = """
INSERT INTO cluster_feature_matrix
  (segment_run_id, cluster, feature, cluster_mean, cluster_median,
   population_mean, population_median, diff, pct_diff, layout)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
ON CONFLICT (segment_run_id, cluster, feature, layout) DO NOTHING
"""

SURROGATE_FIT_UPSERT_SQL = """
INSERT INTO seg_surrogate_fits
  (segment_run_id, accuracy, feature_names, xgb_params)
VALUES ($1, $2, $3::jsonb, $4::jsonb)
ON CONFLICT (segment_run_id) DO NOTHING
"""

LONG_MATRIX_STATS = ("cluster_mean", "cluster_median",
                     "population_mean", "population_median",
                     "diff", "pct_diff")

LONG_MATRIX_UPSERT_SQL = """
INSERT INTO cluster_feature_matrix_long
  (segment_run_id, cluster, feature, stat, value)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (segment_run_id, cluster, feature, stat) DO NOTHING
"""

LONG_MATRIX_FOR_RUN_SQL = """
SELECT cluster, feature, stat, value
FROM cluster_feature_matrix_long
WHERE segment_run_id = $1
ORDER BY cluster, feature, stat
"""

MATRIX_FOR_RUN_SQL = """
SELECT cluster, feature, cluster_mean, cluster_median,
       population_mean, population_median, diff, pct_diff, layout
FROM cluster_feature_matrix
WHERE segment_run_id = $1 AND layout = $2
ORDER BY cluster, feature
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
    model = spec.get("model", "bg_nbd")
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


def _as_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric, got {value!r}")
    return float(value)


def validate_long_row(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate an LM2 long-layout row. Returns a cleaned copy."""
    if not isinstance(spec, dict):
        raise ValueError("long row must be a mapping")
    if not spec.get("segment_run_id"):
        raise ValueError("segment_run_id is required")
    cluster = spec.get("cluster")
    if (isinstance(cluster, bool) or not isinstance(cluster, int)
            or cluster < 0):
        raise ValueError(
            f"cluster must be an int >= 0, got {cluster!r}")
    feature = spec.get("feature")
    if not isinstance(feature, str) or not feature.strip():
        raise ValueError("feature must be a non-empty string")
    stat = spec.get("stat")
    if stat not in LONG_MATRIX_STATS:
        raise ValueError(
            f"stat must be one of {list(LONG_MATRIX_STATS)},"
            f" got {stat!r}")
    value = spec.get("value")
    if value is not None:
        value = _as_number(value, "value")
    return {"segment_run_id": spec["segment_run_id"],
            "cluster": cluster, "feature": feature.strip(),
            "stat": stat, "value": value}


def validate_matrix_row(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate an LM2 comparison-matrix row. Returns a cleaned copy."""
    if not isinstance(spec, dict):
        raise ValueError("matrix row must be a mapping")
    if not spec.get("segment_run_id"):
        raise ValueError("segment_run_id is required")
    cluster = spec.get("cluster")
    if (isinstance(cluster, bool) or not isinstance(cluster, int)
            or cluster < 0):
        raise ValueError(
            f"cluster must be an int >= 0, got {cluster!r}")
    feature = spec.get("feature")
    if not isinstance(feature, str) or not feature.strip():
        raise ValueError("feature must be a non-empty string")
    layout = spec.get("layout", "wide")
    if layout not in ("wide", "long"):
        raise ValueError(
            f"layout must be 'wide' or 'long', got {layout!r}")
    return {"segment_run_id": spec["segment_run_id"],
            "cluster": cluster, "feature": feature.strip(),
            "cluster_mean": _as_number(spec.get("cluster_mean"),
                                         "cluster_mean"),
            "cluster_median": _as_number(spec.get("cluster_median"),
                                           "cluster_median"),
            "population_mean": _as_number(spec.get("population_mean"),
                                            "population_mean"),
            "population_median": _as_number(
                spec.get("population_median"), "population_median"),
            "diff": _as_number(spec.get("diff"), "diff"),
            "pct_diff": (None if spec.get("pct_diff") is None
                           else _as_number(spec.get("pct_diff"),
                                           "pct_diff")),
            "layout": layout}


# The explain-method gate is ml's (`ml_engine/explainer.py:36 EXPLAIN_METHODS`,
# enforced at `ml_engine/store_explain.py:359`). This module used to keep a
# second copy of the admitted labels plus a fidelity gate; `9072b73` deleted
# `xgb_surrogate_v1` from ml and the copy then admitted a method the writer
# refuses -- a validator with no caller that contradicted the only gate. It
# went with the method. What is validated here is the SHAPE of a stored fit row
# (`seg_surrogate_fits` keeps its historic rows; nothing new writes one).


def validate_surrogate_fit(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate an LM1 surrogate-fit row. Returns a cleaned copy."""
    if not isinstance(spec, dict):
        raise ValueError("surrogate fit must be a mapping")
    if not spec.get("segment_run_id"):
        raise ValueError("segment_run_id is required")
    acc = spec.get("accuracy")
    if (isinstance(acc, bool) or not isinstance(acc, (int, float))
            or not 0 <= acc <= 1):
        raise ValueError(
            f"accuracy must be 0..1, got {acc!r}")
    names = spec.get("feature_names", [])
    if (not isinstance(names, list) or not names
            or not all(isinstance(f, str) and f.strip() for f in names)):
        raise ValueError("feature_names must be a non-empty string list")
    params = spec.get("xgb_params", {})
    if not isinstance(params, dict):
        raise ValueError("xgb_params must be a mapping")
    return {"segment_run_id": spec["segment_run_id"],
            "accuracy": float(acc),
            "feature_names": [f.strip() for f in names],
            "xgb_params": dict(params)}


