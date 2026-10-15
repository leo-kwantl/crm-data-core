"""LM1/LM2 writer-discipline tests (data owns DDL, ml owns engines).

Pure: BTYD param keys/positivity, activity/horizon/model enums,
fit-error-without-params, eval metric rows. DB: fit upsert is
idempotent per grain, latest-fit read skips failed fits, eval rows
die with their batch (CASCADE).
"""
from __future__ import annotations

from datetime import date

import pytest

from pipelines.model_fits import validate_assoc_eval, validate_btyd_params

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


def test_assoc_eval_ok_and_rejects():
    out = validate_assoc_eval({"batch_id": "b1", "metric": "precision@5",
                               "value": 0.4, "n_holdout": 200})
    assert out["eval_set"] == "holdout" and out["value"] == 0.4
    with pytest.raises(ValueError, match="batch_id"):
        validate_assoc_eval({"metric": "m", "value": 1.0})
    with pytest.raises(ValueError, match="numeric"):
        validate_assoc_eval({"batch_id": "b", "metric": "m",
                             "value": "high"})


async def test_lm1_lm2_db_roundtrip(database_url: str):
    """Fit upsert idempotent per grain; latest read skips failed fits;
    eval rows cascade with their batch."""
    import json
    import re
    from pathlib import Path

    import asyncpg

    from pipelines.model_fits import (
        ASSOC_EVAL_UPSERT_SQL,
        BTYD_UPSERT_SQL,
        LATEST_BTYD_PARAMS_SQL,
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
                for name in ("018_assoc_rules.sql",
                             "028_btyd_params.sql",
                             "029_assoc_eval.sql"):
                    m = up_re.search(
                        (mig_dir / name).read_text(encoding="utf-8"))
                    assert m is not None
                    await conn.execute(m.group(1).strip())
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
            # LM2: eval rows ride the batch, die with it.
            batch = await conn.fetchval(
                "INSERT INTO assoc_batches DEFAULT VALUES RETURNING id")
            ev = validate_assoc_eval({"batch_id": str(batch),
                                      "metric": "precision@5",
                                      "value": 0.4, "n_holdout": 200})
            from uuid import UUID
            await conn.execute(ASSOC_EVAL_UPSERT_SQL, batch,
                               ev["eval_set"], ev["metric"],
                               ev["value"], ev["n_holdout"])
            await conn.execute(ASSOC_EVAL_UPSERT_SQL, batch,
                               ev["eval_set"], ev["metric"],
                               ev["value"], ev["n_holdout"])  # no-op
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM assoc_rules_eval"
                " WHERE batch_id = $1", batch) == 1
            await conn.execute(
                "DELETE FROM assoc_batches WHERE id = $1", batch)
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM assoc_rules_eval"
                " WHERE batch_id = $1", batch) == 0
            await conn.execute(
                "DELETE FROM btyd_params WHERE as_of >= $1::date", day)
    finally:
        await pool.close()
