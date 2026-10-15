"""LM1/LM2 writer-discipline tests (data owns DDL, ml owns engines).

Pure: BTYD param keys/positivity, activity/horizon/model enums,
fit-error-without-params, matrix row shape. DB: fit upsert is
idempotent per grain, latest-fit read skips failed fits, matrix rows
die with their segment run (CASCADE), run read serves one layout.
"""
from __future__ import annotations

from datetime import date

import pytest

from pipelines.model_fits import (
    LONG_MATRIX_STATS,
    validate_btyd_params,
    validate_long_row,
    validate_matrix_row,
)

pytestmark = pytest.mark.asyncio


def test_btyd_ok_full_fit():
    out = validate_btyd_params({
        "as_of": date(2026, 10, 15), "activity": "orders",
        "horizon_days": 90,
        "params": {"r": 0.5, "alpha": 2.0, "a": 1.0, "b": 3.0},
        "n_customers": 120})
    assert out["model"] == "bg-nbd"
    assert out["params"]["alpha"] == 2.0


def test_btyd_failed_fit_needs_error():
    with pytest.raises(ValueError, match="params or a fit_error"):
        validate_btyd_params({"as_of": date(2026, 10, 15),
                              "activity": "all", "horizon_days": 30,
                              "params": None})
    out = validate_btyd_params({"as_of": date(2026, 10, 15),
                                "activity": "all", "horizon_days": 30,
                                "params": None,
                                "fit_error": "no repeat buyers"})
    assert out["params"] is None


def test_btyd_rejects_bad_params():
    base = {"as_of": date(2026, 10, 15), "activity": "orders",
            "horizon_days": 90}
    with pytest.raises(ValueError, match="missing"):
        validate_btyd_params({**base, "params": {"r": 1.0}})
    with pytest.raises(ValueError, match=r"params\[a\]"):
        validate_btyd_params({**base, "params": {
            "r": 0.5, "alpha": 2.0, "a": -1.0, "b": 3.0}})
    with pytest.raises(ValueError, match="activity"):
        validate_btyd_params({**base, "activity": "clicks",
                              "params": {"r": 0.5, "alpha": 2.0,
                                         "a": 1.0, "b": 3.0}})
    with pytest.raises(ValueError, match="horizon"):
        validate_btyd_params({**base, "horizon_days": 731,
                              "params": {"r": 0.5, "alpha": 2.0,
                                         "a": 1.0, "b": 3.0}})


def test_matrix_row_ok_and_rejects():
    base = {"segment_run_id": "run-1", "cluster": 0,
            "feature": "recency_days", "cluster_mean": 12.5,
            "cluster_median": 10.0, "population_mean": 30.0,
            "population_median": 25.0, "diff": -17.5,
            "pct_diff": -0.58}
    out = validate_matrix_row(dict(base))
    assert out["layout"] == "wide" and out["pct_diff"] == -0.58
    assert validate_matrix_row(
        {**base, "pct_diff": None})["pct_diff"] is None
    with pytest.raises(ValueError, match="segment_run_id"):
        validate_matrix_row({**base, "segment_run_id": None})
    with pytest.raises(ValueError, match="cluster must be"):
        validate_matrix_row({**base, "cluster": -1})
    with pytest.raises(ValueError, match="layout"):
        validate_matrix_row({**base, "layout": "pivoted"})
    with pytest.raises(ValueError, match="numeric"):
        validate_matrix_row({**base, "diff": "lower"})


def test_long_row_ok_and_rejects():
    base = {"segment_run_id": "run-1", "cluster": 1,
            "feature": "orders_90d", "stat": "diff",
            "value": 3.25}
    out = validate_long_row(dict(base))
    assert out["stat"] == "diff" and out["value"] == 3.25
    assert validate_long_row(
        {**base, "value": None})["value"] is None
    with pytest.raises(ValueError, match="stat must be"):
        validate_long_row({**base, "stat": "mean"})
    with pytest.raises(ValueError, match="stat must be"):
        validate_long_row({**base, "stat": "wide"})
    with pytest.raises(ValueError, match="cluster must be"):
        validate_long_row({**base, "cluster": True})
    assert set(LONG_MATRIX_STATS) == {
        "cluster_mean", "cluster_median", "population_mean",
        "population_median", "diff", "pct_diff"}


async def test_lm1_lm2_db_roundtrip(database_url: str):
    """Fit upsert idempotent per grain; latest read skips failed fits;
    matrix rows cascade with their segment run; run read serves layout."""
    import json
    import re
    from pathlib import Path

    import asyncpg

    from pipelines.model_fits import (
        BTYD_UPSERT_SQL,
        LATEST_BTYD_PARAMS_SQL,
        LONG_MATRIX_FOR_RUN_SQL,
        LONG_MATRIX_UPSERT_SQL,
        MATRIX_FOR_RUN_SQL,
        MATRIX_UPSERT_SQL,
    )

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            if await conn.fetchval(
                    "SELECT to_regclass('public.btyd_params')") is None:
                up_re = re.compile(
                    r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)",
                    re.S | re.I)
                mig_dir = (Path(__file__).resolve().parents[3]
                           / "db" / "migrations")
                for name in ("015_segments_pipelines.sql",
                             "028_btyd_params.sql",
                             "029_cluster_matrix.sql",
                             "032_matrix_long.sql"):
                    m = up_re.search(
                        (mig_dir / name).read_text(encoding="utf-8"))
                    assert m is not None
                    try:
                        await conn.execute(m.group(1).strip())
                    except Exception as exc:
                        if "already exists" not in str(exc).lower():
                            raise
            day = date(2026, 10, 15)
            fit = validate_btyd_params({
                "as_of": day, "activity": "orders", "horizon_days": 90,
                "params": {"r": 0.5, "alpha": 2.0, "a": 1.0, "b": 3.0},
                "n_customers": 120, "loglikelihood": -500.25})
            for _ in range(2):  # re-fit same grain = no-op
                await conn.execute(
                    BTYD_UPSERT_SQL, fit["as_of"], fit["activity"],
                    fit["horizon_days"], fit["brand_id"],
                    fit["group_id"], fit["model"],
                    json.dumps(fit["params"]), fit["n_customers"],
                    fit["loglikelihood"], fit["fit_error"])
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM btyd_params"
                " WHERE as_of = $1::date", day)
            assert int(n) == 1
            # Failed fit on another day: stored, but invisible to serving.
            bad = validate_btyd_params({
                "as_of": date(2026, 10, 16), "activity": "orders",
                "horizon_days": 90, "params": None,
                "fit_error": "no repeat buyers"})
            await conn.execute(
                BTYD_UPSERT_SQL, bad["as_of"], bad["activity"],
                bad["horizon_days"], None, None, bad["model"], None,
                0, None, bad["fit_error"])
            latest = await conn.fetch(LATEST_BTYD_PARAMS_SQL)
            mine = [r for r in latest
                    if r["as_of"] == day and r["activity"] == "orders"]
            assert len(mine) == 1
            assert json.loads(mine[0]["params"])["r"] == 0.5
            # LM2: matrix rows ride the segment run, die with it.
            run = await conn.fetchval(
                "INSERT INTO segment_runs (as_of, mode, k)"
                " VALUES ($1::date, 'overall', 3) RETURNING id", day)
            row = validate_matrix_row({
                "segment_run_id": str(run), "cluster": 0,
                "feature": "recency_days", "cluster_mean": 12.5,
                "cluster_median": 10.0, "population_mean": 30.0,
                "population_median": 25.0, "diff": -17.5,
                "pct_diff": -0.58})
            args = (run, row["cluster"], row["feature"],
                    row["cluster_mean"], row["cluster_median"],
                    row["population_mean"], row["population_median"],
                    row["diff"], row["pct_diff"], row["layout"])
            await conn.execute(MATRIX_UPSERT_SQL, *args)
            await conn.execute(MATRIX_UPSERT_SQL, *args)  # no-op
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM cluster_feature_matrix"
                " WHERE segment_run_id = $1", run) == 1
            got = await conn.fetch(MATRIX_FOR_RUN_SQL, run, "wide")
            assert len(got) == 1 and got[0]["feature"] == "recency_days"
            assert await conn.fetch(
                MATRIX_FOR_RUN_SQL, run, "long") == []
            # LM2 long: one row per (cluster x feature x stat).
            long = validate_long_row({
                "segment_run_id": str(run), "cluster": 0,
                "feature": "recency_days", "stat": "diff",
                "value": -17.5})
            for _ in range(2):
                await conn.execute(
                    LONG_MATRIX_UPSERT_SQL, run, long["cluster"],
                    long["feature"], long["stat"], long["value"])
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM cluster_feature_matrix_long"
                " WHERE segment_run_id = $1", run) == 1
            lgot = await conn.fetch(LONG_MATRIX_FOR_RUN_SQL, run)
            assert len(lgot) == 1 and lgot[0]["stat"] == "diff"
            await conn.execute(
                "DELETE FROM segment_runs WHERE id = $1", run)
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM cluster_feature_matrix"
                " WHERE segment_run_id = $1", run) == 0
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM cluster_feature_matrix_long"
                " WHERE segment_run_id = $1", run) == 0
            await conn.execute(
                "DELETE FROM btyd_params WHERE as_of >= $1::date", day)
    finally:
        await pool.close()


def test_surrogate_fit_ok_and_rejects():
    from pipelines.model_fits import (
        validate_explain_method,
        validate_surrogate_fit,
    )
    out = validate_surrogate_fit(
        {"segment_run_id": "run-1", "accuracy": 0.93,
         "feature_names": ["recency_days", "frequency"],
         "xgb_params": {"max_depth": 4}})
    assert out["accuracy"] == 0.93
    with pytest.raises(ValueError, match="0..1"):
        validate_surrogate_fit({"segment_run_id": "r", "accuracy": 1.5,
                                "feature_names": ["f"]})
    with pytest.raises(ValueError, match="feature_names"):
        validate_surrogate_fit({"segment_run_id": "r", "accuracy": 0.9,
                                "feature_names": []})
    assert validate_explain_method("xgb_surrogate_v1") == "xgb_surrogate_v1"
    with pytest.raises(ValueError, match="unknown explain method"):
        validate_explain_method("shap_legacy")


@pytest.mark.asyncio
async def test_lm1_fit_table_and_method_labels(database_url: str):
    """Fit row stores the gate proof; batches/rows default to
    segment_mean; surrogate rows link their fit."""
    import json
    import re
    from pathlib import Path

    import asyncpg

    from pipelines.model_fits import (
        SURROGATE_FIT_UPSERT_SQL,
        validate_surrogate_fit,
    )

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            if await conn.fetchval(
                    "SELECT to_regclass('public.seg_surrogate_fits')"
                    ) is None:
                up_re = re.compile(
                    r"--\s*migrate:up(.*?)(?=--\s*migrate:down|\Z)",
                    re.S | re.I)
                mig_dir = (Path(__file__).resolve().parents[3]
                           / "db" / "migrations")
                for name in ("015_segments_pipelines.sql",
                             "021_explain.sql",
                             "030_surrogate_fits.sql"):
                    m = up_re.search(
                        (mig_dir / name).read_text(encoding="utf-8"))
                    assert m is not None
                    try:
                        await conn.execute(m.group(1).strip())
                    except Exception as exc:
                        if "already exists" not in str(exc).lower():
                            raise
            from datetime import date
            run = await conn.fetchval(
                "INSERT INTO segment_runs (as_of, mode, k)"
                " VALUES ($1::date, 'overall', 3) RETURNING id",
                date(2026, 10, 15))
            fit = validate_surrogate_fit(
                {"segment_run_id": str(run), "accuracy": 0.93,
                 "feature_names": ["recency_days"],
                 "xgb_params": {"max_depth": 4}})
            await conn.execute(
                SURROGATE_FIT_UPSERT_SQL, run, fit["accuracy"],
                json.dumps(fit["feature_names"]),
                json.dumps(fit["xgb_params"]))
            await conn.execute(
                SURROGATE_FIT_UPSERT_SQL, run, fit["accuracy"], "[]",
                "{}")  # refit same run = no-op
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM seg_surrogate_fits"
                " WHERE segment_run_id = $1", run) == 1
            fit_id = await conn.fetchval(
                "SELECT id FROM seg_surrogate_fits"
                " WHERE segment_run_id = $1", run)
            # Legacy-mean batch: defaults label it, no fit link.
            mean_batch = await conn.fetchval(
                "INSERT INTO explain_batches"
                " (segment_run_id, mode, k, top_n)"
                " VALUES ($1, 'overall', 3, 5) RETURNING id", run)
            assert await conn.fetchval(
                "SELECT method FROM explain_batches WHERE id = $1",
                mean_batch) == "segment_mean"
            # Surrogate batch: method + fit ref travel together.
            surr_batch = await conn.fetchval(
                "INSERT INTO explain_batches"
                " (segment_run_id, mode, k, top_n, method, fit_id)"
                " VALUES ($1, 'overall', 3, 5,"
                " 'xgb_surrogate_v1', $2) RETURNING id", run, fit_id)
            await conn.execute(
                "INSERT INTO explain_rows"
                " (batch_id, cluster, rank, feature, mean_abs_shap,"
                " mean_shap, min_shap, max_shap, method)"
                " VALUES ($1, 0, 1, 'recency_days', 0.5, -0.5, -0.6,"
                " -0.4, 'xgb_surrogate_v1')", surr_batch)
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM explain_rows"
                " WHERE batch_id = $1 AND method = 'xgb_surrogate_v1'",
                surr_batch)
            assert int(n) == 1
            await conn.execute(
                "DELETE FROM segment_runs WHERE id = $1", run)
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM seg_surrogate_fits"
                " WHERE segment_run_id = $1", run) == 0
    finally:
        await pool.close()


def test_explain_write_gate():
    from pipelines.model_fits import validate_explain_write
    assert validate_explain_write("segment_mean") == "segment_mean"
    with pytest.raises(ValueError, match="zero shap rows"):
        validate_explain_write("surrogate_unfitted", n_shap_rows=3)
    assert validate_explain_write(
        "surrogate_unfitted") == "surrogate_unfitted"
    with pytest.raises(ValueError, match="surrogate_fits id"):
        validate_explain_write("xgb_surrogate_v1", fit_accuracy=0.95)
    with pytest.raises(ValueError, match=">= 0.90"):
        validate_explain_write(
            "xgb_surrogate_v1", fit_id="f1", fit_accuracy=0.89)
    assert validate_explain_write(
        "xgb_surrogate_v1", fit_id="f1",
        fit_accuracy=0.93) == "xgb_surrogate_v1"
    with pytest.raises(ValueError, match="unknown explain method"):
        validate_explain_write("shap_legacy")
