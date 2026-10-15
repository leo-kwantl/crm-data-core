"""Pipeline scheduler tests (015 tables + pipelines/schedule.py).

Unit: manifest parse, topo sort, ml argv builders. DB: segment
idempotency, manifest seeds, runner skip/force/failure semantics with
fake engines (no ml checkout needed).

DB tests open their own pool per test (repo pattern from test_upload:
the session pool binds to a single event loop).
"""

from __future__ import annotations

import sys
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import pytest

from pipelines import schedule as sched

REPO = Path(__file__).resolve().parents[3]
FAKE_UUID = "12345678-1234-5678-1234-567812345678"


@asynccontextmanager
async def _pool(database_url: str):
    import asyncpg

    pool = await asyncpg.create_pool(database_url)
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


def _ctx(**kw):
    base = {"pipeline": "test_pipe", "as_of": date(2026, 10, 1),
            "brand_id": None, "group_id": None, "created_by": "test",
            "ml_repo": REPO, "db_url": "postgresql://fake/db"}
    base.update(kw)
    return sched.JobCtx(**base)


# ---- manifest parse + topo ----

def test_seed_file_mentions_expected_engines():
    text = (REPO / "db" / "migrations" / "015_segments_pipelines.sql"
            ).read_text(encoding="utf-8")
    assert "upload_apply" in text and "recs_batch" in text


def test_topo_sort_orders_and_detects_cycles():
    jobs = {"a": {"engine": "e", "depends_on": [], "params": {}},
            "b": {"engine": "e", "depends_on": ["a"], "params": {}},
            "c": {"engine": "e", "depends_on": ["a"], "params": {}},
            "d": {"engine": "e", "depends_on": ["b", "c"], "params": {}}}
    order = sched.topo_sort(jobs)
    assert order[0] == "a" and order[-1] == "d"
    assert set(order) == {"a", "b", "c", "d"}
    with pytest.raises(sched.ManifestError, match="cycle"):
        sched.topo_sort({
            "x": {"engine": "e", "depends_on": ["y"], "params": {}},
            "y": {"engine": "e", "depends_on": ["x"], "params": {}}})
    with pytest.raises(sched.ManifestError, match="unknown job"):
        sched.topo_sort(
            {"x": {"engine": "e", "depends_on": ["nope"], "params": {}}})
    with pytest.raises(sched.ManifestError, match="engine"):
        sched.parse_manifest("pipelines:\n  p:\n    jobs:\n      j: {}\n")
    with pytest.raises(sched.ManifestError, match="pipelines"):
        sched.parse_manifest("version: 1\n")


def test_parse_skips_disabled():
    jobs = sched.parse_manifest(
        "pipelines:\n  p:\n    jobs:\n"
        "      job_on: {engine: e}\n"
        "      job_off: {engine: e, enabled: false}\n")
    assert set(jobs["p"]) == {"job_on"}


# ---- ml argv builders (exact contract) ----

def test_ml_argv_builders():
    ctx = _ctx(as_of=date(2026, 9, 1))
    db = "postgresql://fake/db"
    out = sched.ENGINE_BUILDERS["churn_snapshot"](
        {"activity": "orders", "horizon_days": 90}, ctx)
    assert out == [sys.executable, "-m", "ml_jobs.run_churn_snapshot", db,
                   "--activity", "orders", "--horizon-days", "90",
                   "--model", "bgnbd",
                   "--as-of", "2026-09-01", "--created-by", "test"], out
    out = sched.ENGINE_BUILDERS["clv_snapshot"](
        {"horizon_days": 90, "model": "heuristic"}, ctx)
    assert out[:4] == [sys.executable, "-m", "ml_jobs.run_clv_snapshot", db]
    assert "--horizon-days" in out and "--as-of" in out
    assert out[out.index("--model") + 1] == "heuristic"
    out = sched.ENGINE_BUILDERS["rfm_snapshot"]({"mode": "rolling_365"}, ctx)
    assert out[:4] == [sys.executable, "-m", "ml_jobs.run_snapshot", db]
    assert out[4:6] == ["--mode", "rolling_365"]
    out = sched.ENGINE_BUILDERS["recs_batch"](
        {"top_n": 10, "min_support": 2, "min_confidence": 0.2}, ctx)
    assert out[:4] == [sys.executable, "-m", "ml_jobs.run_recs_batch", db]
    assert "--as-of" not in out  # recs takes no --as-of
    assert out[out.index("--top-n") + 1] == "10"
    assert out[out.index("--content") + 1] == "off"
    assert "--include-user-cf" not in out
    out = sched.ENGINE_BUILDERS["recs_batch"](
        {"include_user_cf": True, "content": "tfidf"}, ctx)
    assert "--include-user-cf" in out
    assert out[out.index("--content") + 1] == "tfidf"
    out = sched.ENGINE_BUILDERS["segments"](
        {"mode": "behavior", "k": 6, "seed": 3,
         "algorithm": "gmm"}, ctx)
    assert out[:4] == [sys.executable, "-m",
                       "ml_jobs.run_segments_snapshot", db]
    assert out[4:12] == ["--mode", "behavior", "--k", "6", "--seed",
                          "3", "--algorithm", "gmm"]
    ctx2 = _ctx(brand_id=uuid.uuid4())
    out = sched.ENGINE_BUILDERS["clv_snapshot"]({}, ctx2)
    assert out[out.index("--brand-id") + 1] == str(ctx2.brand_id)


# ---- DB: 015 tables ----

@pytest.mark.asyncio
async def test_segment_tables_idempotent(database_url: str):
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            rid = await conn.fetchval(
                "INSERT INTO segment_runs (as_of, mode, k, seed, centroids,"
                " features, inertia, customer_count, rules_version)"
                " VALUES ('2026-10-01', 'purchase', 6, 3, '[]', '[]', 1.5,"
                " 0, 'v1') RETURNING id")
            assert rid is not None
            dup = await conn.fetchval(
                "INSERT INTO segment_runs (as_of, mode, k, seed)"
                " VALUES ('2026-10-01', 'purchase', 6, 3)"
                " ON CONFLICT DO NOTHING RETURNING id")
            assert dup is None  # same day x mode x k x scope = no-op
            other = await conn.fetchval(
                "INSERT INTO segment_runs (as_of, mode, k)"
                " VALUES ('2026-10-01', 'overall', 6) RETURNING id")
            assert other is not None and str(other) != str(rid)
            behavior = await conn.fetchval(
                "INSERT INTO segment_runs (as_of, mode, k)"
                " VALUES ('2026-10-02', 'behavior', 4) RETURNING id")
            assert behavior is not None  # 016 widens the mode CHECK


@pytest.mark.asyncio
async def test_manifest_seeds_parse_and_order(database_url: str):
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT pipeline, yaml FROM pipeline_manifests"
                " WHERE pipeline IN ('nightly', 'weekly')"
                " ORDER BY pipeline")
    assert [r["pipeline"] for r in rows] == ["nightly", "weekly"]
    manifests = sched.parse_manifest(rows[0]["yaml"])
    manifests.update(sched.parse_manifest(rows[1]["yaml"]))
    nightly = sched.topo_sort(manifests["nightly"])
    assert nightly[0] == "apply"  # everything else depends on apply
    assert set(nightly) == {"apply", "rfm", "churn", "clv"}


# ---- runner with fake engines ----

def _fake_ok(params, ctx):
    return [sys.executable, "-c", f"print('{FAKE_UUID}')"]


def _fake_fail(params, ctx):
    return [sys.executable, "-c", "import sys; sys.exit(1)"]


@pytest.mark.asyncio
async def test_runner_success_skip_force(database_url: str, monkeypatch):
    monkeypatch.setitem(sched.ENGINE_BUILDERS, "fake_ok", _fake_ok)
    jobs = {"j1": {"engine": "fake_ok", "depends_on": [], "params": {}}}
    ctx = _ctx(pipeline="test_pipe_ok", as_of=date(2026, 10, 2))
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            out = await sched.run_pipeline(conn, "test_pipe_ok", jobs, ctx)
            assert out["outcomes"]["j1"]["status"] == "succeeded"
            assert str(out["outcomes"]["j1"]["run_id"]) == FAKE_UUID
            out2 = await sched.run_pipeline(conn, "test_pipe_ok", jobs, ctx)
            assert out2["outcomes"]["j1"]["status"] == "skipped"
            out3 = await sched.run_pipeline(conn, "test_pipe_ok", jobs, ctx,
                                            force=True)
            assert out3["outcomes"]["j1"]["status"] == "succeeded"
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM pipeline_runs"
                " WHERE pipeline = 'test_pipe_ok' AND job = 'j1'")
            assert int(n) == 1  # one row per job x as_of x scope (upserted)


@pytest.mark.asyncio
async def test_runner_failure_skips_downstream(database_url: str,
                                               monkeypatch):
    monkeypatch.setitem(sched.ENGINE_BUILDERS, "fake_fail", _fake_fail)
    monkeypatch.setitem(sched.ENGINE_BUILDERS, "fake_ok", _fake_ok)
    jobs = {"bad": {"engine": "fake_fail", "depends_on": [], "params": {}},
            "down": {"engine": "fake_ok", "depends_on": ["bad"],
                     "params": {}},
            "mystery": {"engine": "no_such_engine", "depends_on": [],
                        "params": {}}}
    ctx = _ctx(pipeline="test_pipe_fail", as_of=date(2026, 10, 3))
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            out = await sched.run_pipeline(conn, "test_pipe_fail", jobs, ctx)
            assert out["outcomes"]["bad"]["status"] == "failed"
            assert out["outcomes"]["down"]["status"] == "skipped"
            assert "upstream" in out["outcomes"]["down"]["error"]
            assert out["outcomes"]["mystery"]["status"] == "failed"
            assert "no argv builder" in out["outcomes"]["mystery"]["error"]


@pytest.mark.asyncio
async def test_runner_data_engines(database_url: str):
    jobs = {"apply": {"engine": "upload_apply", "depends_on": [],
                      "params": {"strategy": "validated_only"}},
            "kpi": {"engine": "kpi_daily", "depends_on": [],
                    "params": {}}}
    ctx = _ctx(pipeline="test_pipe_data", as_of=date(2026, 10, 4))
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            out = await sched.run_pipeline(conn, "test_pipe_data", jobs, ctx)
            assert out["outcomes"]["apply"]["status"] == "succeeded"
            assert out["outcomes"]["kpi"]["status"] == "succeeded"
            assert out["outcomes"]["kpi"]["run_id"] is not None
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM kpi_daily_snapshots"
                " WHERE snapshot_date = '2026-10-04'")
            assert int(n) == 1


# ---- scheduler-owned freshness: cohort engine + new ml builders ----

def test_data_engines_registry():
    assert {"upload_apply", "kpi_daily", "cohort_snapshot"} <= set(
        sched.DATA_ENGINES)
    for e in ("rfm_snapshot", "forecast_snapshot", "assoc_batch",
              "explain_batch"):
        assert e in sched.ENGINE_BUILDERS


def test_build_forecast_assoc_explain_argv():
    ctx = _ctx()
    f = sched.ENGINE_BUILDERS["forecast_snapshot"]({}, ctx)
    assert f[1:4] == ["-m", "ml_jobs.run_forecast_snapshot",
                      "postgresql://fake/db"]
    assert "--horizon-days" in f and "28" in f
    assert "--lookback-weeks" in f and "52" in f
    assert "--as-of" in f
    a = sched.ENGINE_BUILDERS["assoc_batch"]({}, ctx)
    assert a[1:3] == ["-m", "ml_jobs.run_assoc_batch"]
    assert "--min-support" in a and "5" in a
    assert "--min-rules" in a and "50" in a
    assert "--as-of" not in a
    e = sched.ENGINE_BUILDERS["explain_batch"](
        {"segment_run_id": "abc"}, ctx)
    assert e[1:3] == ["-m", "ml_jobs.run_explain_batch"]
    assert "--segment-run-id" in e and "abc" in e
    assert "--top-n" in e and "20" in e
    assert "--as-of" not in e


def test_build_explain_requires_run_id():
    with pytest.raises(sched.ManifestError, match="segment_run_id"):
        sched.ENGINE_BUILDERS["explain_batch"]({}, _ctx())


@pytest.mark.asyncio
async def test_cohort_invalid_granularity_rejected():
    with pytest.raises(sched.ManifestError, match="granularities"):
        await sched._run_data_engine(
            "cohort_snapshot", {"granularities": ["fortnight"]},
            None, _ctx())


@pytest.mark.asyncio
async def test_unknown_data_engine_rejected():
    with pytest.raises(sched.UnknownEngineError,
                       match="unknown data engine"):
        await sched._run_data_engine("nope", {}, None, _ctx())


@pytest.mark.asyncio
async def test_resolve_explain_run_id(database_url: str):
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            old = await conn.fetchval(
                "INSERT INTO segment_runs (as_of, mode, k, created_at)"
                " VALUES ('2025-05-01', 'purchase', 5,"
                " '2025-05-02 00:00+00') RETURNING id")
            new = await conn.fetchval(
                "INSERT INTO segment_runs (as_of, mode, k, created_at)"
                " VALUES ('2025-06-01', 'purchase', 5,"
                " '2025-06-02 00:00+00') RETURNING id")
            ctx = _ctx()
            assert await sched._resolve_explain_run_id(
                conn, {"segment_run_id": str(old)}, ctx) == str(old)
            # scoped to purchase/k=5: newest of the two seeded rows.
            assert await sched._resolve_explain_run_id(
                conn, {"segment_run_id": "latest",
                       "segment_mode": "purchase",
                       "segment_k": 5}, ctx) == str(new)
            assert await sched._resolve_explain_run_id(
                conn, {"segment_mode": "purchase", "segment_k": 5},
                ctx) == str(new)
            # bare latest (no k) may land on another test's newer
            # purchase row in the shared DB — assert UUID shape only.
            bare = await sched._resolve_explain_run_id(
                conn, {"segment_run_id": "latest",
                       "segment_mode": "purchase"}, ctx)
            assert uuid.UUID(bare) is not None
            # unmatched scope -> actionable ManifestError (also proves
            # resolution is scope-aware; the shared DB may hold any mode).
            scoped = _ctx(brand_id=str(uuid.uuid4()))
            with pytest.raises(sched.ManifestError, match="no segment_runs"):
                await sched._resolve_explain_run_id(
                    conn, {"segment_run_id": "latest",
                           "segment_mode": "purchase",
                           "segment_k": 5}, scoped)
            assert old != new
