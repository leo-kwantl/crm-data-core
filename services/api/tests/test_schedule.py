"""Pipeline scheduler tests (015 tables + pipelines/schedule.py).

Unit: manifest parse, topo sort, ml argv builders. DB: segment
idempotency, manifest seeds, runner skip/force/failure semantics with
fake engines (no ml checkout needed).

DB tests open their own pool per test (repo pattern from test_upload:
the session pool binds to a single event loop).
"""

from __future__ import annotations

import json
import subprocess
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


def test_seed_segments_jobs_pin_serving_default_k():
    """The cadence must fit the k that serving reads by default.

    Serving serves the LATEST run for (mode, k, scope) and defaults to
    k=5, so a segments job that omits k (or picks another value) leaves
    the product's default surface dark after a wipe while every job
    reports success. Parse the seed's own YAML -- a text grep would not
    fail if a param were dropped.
    """
    import re

    import yaml

    text = (REPO / "db" / "migrations" / "015_segments_pipelines.sql"
            ).read_text(encoding="utf-8")
    body = re.search(r"VALUES \('weekly', \$\$(.*?)\$\$", text, re.S)
    assert body, "weekly manifest seed not found"
    jobs = yaml.safe_load(body.group(1))["pipelines"]["weekly"]["jobs"]
    segs = {n: j for n, j in jobs.items() if j.get("engine") == "segments"}
    assert segs, "weekly seed has no segments jobs"
    ks = set()
    for name, job in segs.items():
        k = (job.get("params") or {}).get("k")
        assert isinstance(k, int) and not isinstance(k, bool), \
            f"{name} must pin k explicitly, got {k!r}"
        ks.add(k)
    assert 5 in ks, f"cadence must fit serving's default k=5, got {ks}"


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


def _init_repo(path: Path) -> str:
    """Minimal git checkout with one commit; returns its full sha."""
    def _git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(path), *args],
                              capture_output=True, text=True,
                              check=True).stdout

    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q")
    (path / "engine.py").write_text("x = 1\n", encoding="utf-8")
    _git("add", "-A")
    _git("-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-qm", "init")
    return _git("rev-parse", "HEAD").strip()


@pytest.mark.asyncio
async def test_runner_labels_ml_checkout_revision(database_url: str,
                                                  monkeypatch,
                                                  tmp_path: Path):
    """ml rows carry the checkout's revision; best-effort, never a gate."""
    monkeypatch.setitem(sched.ENGINE_BUILDERS, "fake_ok", _fake_ok)
    repo = tmp_path / "ml"
    head = _init_repo(repo)
    jobs = {"j1": {"engine": "fake_ok", "depends_on": [], "params": {}}}
    ctx = _ctx(pipeline="test_pipe_mlrev", as_of=date(2026, 10, 5),
               ml_repo=repo)
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            async def _upstream(pipeline: str) -> dict:
                raw = await conn.fetchval(
                    "SELECT upstream FROM pipeline_runs"
                    " WHERE pipeline = $1 AND job = 'j1'", pipeline)
                return json.loads(raw) if isinstance(raw, str) else raw

            out = await sched.run_pipeline(conn, "test_pipe_mlrev", jobs, ctx)
            assert out["outcomes"]["j1"]["status"] == "succeeded"
            up = await _upstream("test_pipe_mlrev")
            assert up["engine"] == "fake_ok"
            assert up["ml_repo_sha"] == head
            assert up["ml_repo_dirty"] is False

            # a dirty checkout has NO revision: the sha stays, the flag says so
            (repo / "engine.py").write_text("x = 2\n", encoding="utf-8")
            await sched.run_pipeline(conn, "test_pipe_mlrev", jobs, ctx,
                                     force=True)
            up = await _upstream("test_pipe_mlrev")
            assert up["ml_repo_sha"] == head and up["ml_repo_dirty"] is True

            # unresolvable checkout: nulls, and the tick still records the job
            await sched.run_pipeline(
                conn, "test_pipe_norepo", jobs,
                _ctx(pipeline="test_pipe_norepo", as_of=date(2026, 10, 5),
                     ml_repo=tmp_path / "not-a-repo"))
            up = await _upstream("test_pipe_norepo")
            assert up["ml_repo_sha"] is None
            assert up["ml_repo_dirty"] is None


@pytest.mark.asyncio
async def test_runner_data_rows_carry_no_ml_revision(database_url: str):
    """A revision on an in-process data row would be a mislabel."""
    jobs = {"apply": {"engine": "upload_apply", "depends_on": [],
                      "params": {"strategy": "validated_only"}}}
    ctx = _ctx(pipeline="test_pipe_norev", as_of=date(2026, 10, 6),
               ml_repo=REPO)
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            await sched.run_pipeline(conn, "test_pipe_norev", jobs, ctx)
            raw = await conn.fetchval(
                "SELECT upstream FROM pipeline_runs"
                " WHERE pipeline = 'test_pipe_norev' AND job = 'apply'")
    assert json.loads(raw) == {"engine": "upload_apply"}


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
    for e in ("rfm_snapshot", "ld8_snapshot", "forecast_snapshot",
              "assoc_batch", "explain_batch", "matrix_batch"):
        assert e in sched.ENGINE_BUILDERS


def test_build_ld8_snapshot_argv():
    # ml CLI (run_ld8_snapshot.py) declares DB_URL, --as-of, scope and
    # --created-by; the four windows are fixed, so there is no --mode.
    ctx = _ctx(as_of=date(2026, 10, 17))
    out = sched.ENGINE_BUILDERS["ld8_snapshot"]({}, ctx)
    assert out == [sys.executable, "-m", "ml_jobs.run_ld8_snapshot",
                   "postgresql://fake/db",
                   "--as-of", "2026-10-17", "--created-by", "test"], out


def test_build_ld8_snapshot_rejects_legacy_window_params():
    # A nightly job left on the legacy single-window params must fail with
    # the fix named: passing --mode to that CLI is an argparse exit 2 on
    # every tick, and an append-only v1 row lands until it is corrected.
    for legacy in ({"mode": "rolling_365"},
                   {"date_from": "2026-08-01"},
                   {"date_to": "2026-10-17"}):
        with pytest.raises(sched.ManifestError, match="ld8_snapshot"):
            sched.ENGINE_BUILDERS["ld8_snapshot"](legacy, _ctx())


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


def test_build_matrix_argv_is_run_id_only():
    # ml CLI (run_matrix_batch.py) declares DB_URL + --segment-run-id
    # and nothing else; any extra flag = exit 2 on every tick.
    scoped = _ctx(brand_id="11111111-1111-1111-1111-111111111111",
                  group_id="22222222-2222-2222-2222-222222222222")
    m = sched.ENGINE_BUILDERS["matrix_batch"](
        {"segment_run_id": "abc"}, scoped)
    assert m[1:4] == ["-m", "ml_jobs.run_matrix_batch",
                      "postgresql://fake/db"]
    assert "--segment-run-id" in m and "abc" in m
    for flag in ("--brand-id", "--group-id", "--created-by",
                 "--as-of"):
        assert flag not in m


def test_build_matrix_requires_run_id():
    with pytest.raises(sched.ManifestError, match="segment_run_id"):
        sched.ENGINE_BUILDERS["matrix_batch"]({}, _ctx())


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
            # matrix_batch shares the resolver; engine name only labels
            # the failure message.
            assert await sched._resolve_segment_run_id(
                conn, {"segment_run_id": "latest",
                       "segment_mode": "purchase",
                       "segment_k": 5}, ctx, "matrix_batch") == str(new)
            with pytest.raises(sched.ManifestError,
                               match="matrix_batch: no segment_runs"):
                await sched._resolve_segment_run_id(
                    conn, {"segment_run_id": "latest",
                           "segment_mode": "purchase",
                           "segment_k": 5}, scoped, "matrix_batch")
            assert old != new


# ---- explain scope-strip (ml review bug) + tick-time DB resolution ----

def test_explain_argv_strips_scope_flags():
    scoped = _ctx(brand_id="11111111-1111-1111-1111-111111111111",
                  group_id="22222222-2222-2222-2222-222222222222")
    e = sched.ENGINE_BUILDERS["explain_batch"](
        {"segment_run_id": "latest"}, scoped)
    assert "--brand-id" not in e and "--group-id" not in e
    assert "--created-by" in e and "test" in e
    # assoc DOES accept scope flags — untouched.
    a = sched.ENGINE_BUILDERS["assoc_batch"]({}, scoped)
    assert "--brand-id" in a and "11111111-1111-1111-1111-111111111111" in a


def test_window_rolling_days():
    end = date(2026, 10, 15)
    assert sched.resolve_window("rolling", 30, "days", end) == (
        date(2026, 9, 16), end)


def test_window_rolling_guard_rejects_short():
    with pytest.raises(ValueError):
        sched.resolve_window("rolling", 7, "days", date(2026, 10, 15))
    with pytest.raises(ValueError):
        sched.resolve_window("rolling", 3, "weeks", date(2026, 10, 15))


def test_window_past_months_excludes_current():
    # Oct 15 -> past 1 month = all of September
    assert sched.resolve_window("past", 1, "months",
                                date(2026, 10, 15)) == (
        date(2026, 9, 1), date(2026, 9, 30))


def test_window_past_weeks_are_mon_sun():
    # Wed 2026-10-14 -> past 1 week = Mon 10/5 .. Sun 10/11
    assert sched.resolve_window("past", 1, "weeks",
                                date(2026, 10, 14)) == (
        date(2026, 10, 5), date(2026, 10, 11))


def test_window_current_month_to_date():
    assert sched.resolve_window("current", None, "months",
                                date(2026, 10, 15)) == (
        date(2026, 10, 1), date(2026, 10, 15))


def test_window_rejects_unknown():
    with pytest.raises(ValueError):
        sched.resolve_window("someday", 30, "days", date(2026, 10, 15))
    with pytest.raises(ValueError):
        sched.resolve_window("rolling", 30, "fortnights",
                             date(2026, 10, 15))


@pytest.mark.asyncio
async def test_runner_records_sync_lineage(database_url: str, monkeypatch):
    """LD3: pipeline_runs.sync_run_id links a tick to its feeding sync;
    re-runs without the link preserve it (COALESCE, never nulled)."""
    monkeypatch.setitem(sched.ENGINE_BUILDERS, "fake_ok", _fake_ok)
    jobs = {"j1": {"engine": "fake_ok", "depends_on": [], "params": {}}}
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            cid = await conn.fetchval(
                "INSERT INTO connectors (source_type, name)"
                " VALUES ('csv', 'ld3-probe') RETURNING id")
            sid = await conn.fetchval(
                "INSERT INTO connector_sync_runs"
                " (connector_id, entity, status)"
                " VALUES ($1, 'orders', 'succeeded') RETURNING id", cid)
            ctx = _ctx(pipeline="test_pipe_ld3", as_of=date(2026, 10, 3),
                       sync_run_id=str(sid))
            out = await sched.run_pipeline(conn, "test_pipe_ld3", jobs, ctx)
            assert out["outcomes"]["j1"]["status"] == "succeeded"
            got = await conn.fetchval(
                "SELECT sync_run_id FROM pipeline_runs"
                " WHERE pipeline = 'test_pipe_ld3' AND job = 'j1'")
            assert str(got) == str(sid)
            # force re-run WITHOUT the link: existing link preserved
            ctx2 = _ctx(pipeline="test_pipe_ld3", as_of=date(2026, 10, 3))
            out2 = await sched.run_pipeline(conn, "test_pipe_ld3", jobs,
                                            ctx2, force=True)
            assert out2["outcomes"]["j1"]["status"] == "succeeded"
            got2 = await conn.fetchval(
                "SELECT sync_run_id FROM pipeline_runs"
                " WHERE pipeline = 'test_pipe_ld3' AND job = 'j1'")
            assert str(got2) == str(sid)


@pytest.mark.asyncio
async def test_sync_lineage_stamps_agree(database_url: str, monkeypatch):
    """The one stamped->stamped edge data owns: pipeline_runs vs its feeding
    connector_sync_runs row. The runner takes both stamps straight from the
    caller's flags (scripts/run_pipeline.py:113-116) and never reads the sync
    row, so nothing structural holds them equal -- this test is what does,
    and it is the only harness the edge can have (its writer is data's)."""
    monkeypatch.setitem(sched.ENGINE_BUILDERS, "fake_ok", _fake_ok)
    jobs = {"j1": {"engine": "fake_ok", "depends_on": [], "params": {}}}
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            brand = await conn.fetchval("SELECT id FROM brands LIMIT 1")
            group = await conn.fetchval("SELECT id FROM groups LIMIT 1")
            if brand is None or group is None:
                pytest.skip("seeded brands/groups required for a stamped run")
            cid = await conn.fetchval(
                "INSERT INTO connectors (source_type, name)"
                " VALUES ('csv', 'ld3-stamps-probe') RETURNING id")
            sid = await conn.fetchval(
                "INSERT INTO connector_sync_runs"
                " (connector_id, entity, status, brand_id, group_id)"
                " VALUES ($1, 'orders', 'succeeded', $2, $3) RETURNING id",
                cid, brand, group)
            ctx = _ctx(pipeline="test_pipe_ld3_stamps",
                       as_of=date(2026, 10, 5), brand_id=brand,
                       group_id=group, sync_run_id=str(sid))
            out = await sched.run_pipeline(
                conn, "test_pipe_ld3_stamps", jobs, ctx)
            assert out["outcomes"]["j1"]["status"] == "succeeded"
            run = await conn.fetchrow(
                "SELECT brand_id, group_id, sync_run_id FROM pipeline_runs"
                " WHERE pipeline = 'test_pipe_ld3_stamps' AND job = 'j1'")
            sync = await conn.fetchrow(
                "SELECT brand_id, group_id FROM connector_sync_runs"
                " WHERE id = $1", sid)
            assert (run["brand_id"], run["group_id"]) == (
                sync["brand_id"], sync["group_id"])
            assert str(run["sync_run_id"]) == str(sid)
