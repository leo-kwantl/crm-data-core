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
from datetime import date, datetime, timedelta, timezone
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


@pytest.fixture(autouse=True)
def _own_rows(database_url: str):
    """Delete this file's own rows before each test, so history cannot decide.

    These tests write `pipeline_runs` rows named `test_pipe_*`, probe
    connectors named `ld3-*-probe`, and `segment_runs` at four fixed dates. On
    a database with history (the demo cluster) a leftover row from an earlier
    run is *claimed* by the gate in `UPSERT_RUN_SQL` (`skipped`, not
    `succeeded`) or collides on a unique index, so a re-run failed for a reason
    that had nothing to do with the code under test. Owning the rows is the fix:
    a test must not depend on the database being fresh.
    run; they scope the resolver test to `purchase/k=777`, a k no demo run uses.
    """
    import asyncio

    async def _clean() -> None:
        import asyncpg

        conn = await asyncpg.connect(database_url)
        try:
            await conn.execute(
                "DELETE FROM pipeline_runs WHERE pipeline LIKE 'test_pipe%'")
            await conn.execute("DELETE FROM connectors WHERE name LIKE 'ld3-%'")
            await conn.execute(
                "DELETE FROM segment_runs"
                " WHERE COALESCE(brand_id, group_id) IS NULL"
                " AND ((as_of IN ('2026-10-01', '2026-10-02')"
                "       AND mode IN ('purchase', 'overall', 'behavior'))"
                "      OR (mode = 'purchase' AND k = 777))")
        finally:
            await conn.close()

    asyncio.run(_clean())
    yield


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


# ---- due-window decision (the tick's cron) ----

HK = timezone(timedelta(hours=8))   # business clock, as business_now()
LIVE_NIGHTLY = "0 1 * * *"          # live row, nightly v3
LIVE_WEEKLY = "0 2 * * 1"           # live row, weekly v3


def test_cron_matches_the_live_schedules():
    assert sched.cron_matches(LIVE_NIGHTLY,
                              datetime(2026, 10, 17, 1, 0, tzinfo=HK))
    assert not sched.cron_matches(LIVE_NIGHTLY,
                                  datetime(2026, 10, 17, 1, 1, tzinfo=HK))
    monday = datetime(2026, 10, 19, 2, 0, tzinfo=HK)
    assert monday.weekday() == 0                    # 0 = Sunday in cron
    assert sched.cron_matches(LIVE_WEEKLY, monday)
    assert not sched.cron_matches(LIVE_WEEKLY,
                                  datetime(2026, 10, 20, 2, 0, tzinfo=HK))
    assert not sched.cron_matches(LIVE_WEEKLY,
                                  datetime(2026, 10, 19, 3, 0, tzinfo=HK))
    sunday = datetime(2026, 10, 18, 2, 0, tzinfo=HK)
    assert sunday.weekday() == 6
    assert sched.cron_matches("0 2 * * 0", sunday)
    assert sched.cron_matches("0 2 * * 7", sunday)  # 7 is Sunday too


def test_cron_field_shapes():
    when = datetime(2026, 10, 17, 1, 15, tzinfo=HK)
    assert sched.cron_matches("*/15 * * * *", when)
    assert sched.cron_matches("15 * * * *", when)
    assert sched.cron_matches("0-29 * * * *", when)
    assert sched.cron_matches("0,15,30 * * * *", when)
    assert not sched.cron_matches("*/20 * * * *", when)   # 15 off-step
    assert sched.cron_matches("* * * * *", when)
    # dom/dow are ORed when both are restricted (Vixie), never ANDed: a
    # manifest needing both must say so in a job param, not in the cron
    first_sunday = datetime(2026, 11, 1, 0, 0, tzinfo=HK)
    assert first_sunday.weekday() == 6 and first_sunday.day == 1
    assert sched.cron_matches("0 0 1 * 1", first_sunday)
    # Tuesday the 2nd: neither the 1st nor a Monday, so the OR fails
    assert not sched.cron_matches("0 0 1 * 1",
                                  datetime(2026, 12, 2, 0, 0, tzinfo=HK))


def test_cron_rejects_a_schedule_it_cannot_read():
    when = datetime(2026, 10, 17, 1, 0, tzinfo=HK)
    for bad in ("0 1 * *", "0 1 * * * *", "99 1 * * *", "0 25 * * *",
                "0 1 0 * *", "0 1 * 13 *", "0 1 * * 8", "daily",
                "0 1 * * mon", "*/0 * * * *"):
        with pytest.raises(sched.ManifestError):
            sched.cron_matches(bad, when)


MANIFEST_WITH_SCHEDULE = (
    "version: 1\n"
    "pipelines:\n"
    "  nightly:\n"
    "    schedule: 0 1 * * *\n"
    "    jobs:\n"
    "      apply: {engine: upload_apply}\n")


def test_due_pipelines_reads_the_declared_schedule():
    assert sched.due_pipelines(
        MANIFEST_WITH_SCHEDULE,
        datetime(2026, 10, 17, 1, 0, tzinfo=HK)) == ["nightly"]
    assert sched.due_pipelines(
        MANIFEST_WITH_SCHEDULE,
        datetime(2026, 10, 17, 1, 1, tzinfo=HK)) == []
    # no schedule = manual only: the tick must not invent a cadence
    manual = MANIFEST_WITH_SCHEDULE.replace("    schedule: 0 1 * * *\n", "")
    assert sched.due_pipelines(
        manual, datetime(2026, 10, 17, 1, 0, tzinfo=HK)) == []


def test_due_pipelines_reports_a_bad_schedule_per_manifest():
    bad = MANIFEST_WITH_SCHEDULE.replace("0 1 * * *", "0 1 * *")
    with pytest.raises(sched.ManifestError):
        sched.due_pipelines(bad, datetime(2026, 10, 17, 1, 0, tzinfo=HK))


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
    assert set(nightly) == {"apply", "rfm", "churn", "clv",
                            "segments_purchase", "segments_overall",
                            "segments_behavior"}
    # 041: the two derived segment modes wait for the purchase run.
    assert (nightly.index("segments_purchase")
            < nightly.index("segments_overall"))
    assert (nightly.index("segments_purchase")
            < nightly.index("segments_behavior"))


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
async def test_runner_labels_a_failed_job_too(database_url: str, monkeypatch,
                                              tmp_path: Path):
    """A failed row still says which code ran -- that is when it matters.

    The label is resolved before the engine is invoked (`_upstream_payload` `pipelines/schedule.py:819`)
    and both finish paths persist it (`FINISH_RUN_SQL` `pipelines/schedule.py:118`, `:903`, `:910`), so a non-zero exit cannot leave
    an error whose revision is unknown.
    """
    monkeypatch.setitem(sched.ENGINE_BUILDERS, "fake_fail", _fake_fail)
    repo = tmp_path / "ml"
    head = _init_repo(repo)
    jobs = {"bad": {"engine": "fake_fail", "depends_on": [], "params": {}}}
    ctx = _ctx(pipeline="test_pipe_faillabel", as_of=date(2026, 10, 5),
               ml_repo=repo)
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            out = await sched.run_pipeline(conn, "test_pipe_faillabel", jobs,
                                           ctx)
            assert out["outcomes"]["bad"]["status"] == "failed"
            row = await conn.fetchrow(
                "SELECT status, upstream, error FROM pipeline_runs"
                " WHERE pipeline = $1 AND job = 'bad'",
                "test_pipe_faillabel")
            raw = row["upstream"]
            up = json.loads(raw) if isinstance(raw, str) else raw
            assert row["status"] == "failed"
            assert up["ml_repo_sha"] == head   # revision survives failure
            assert up["ml_repo_dirty"] is False
            assert row["error"]                # and the error is recorded too


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
            before = await conn.fetchval(
                "SELECT COUNT(*) FROM kpi_daily_snapshots"
                " WHERE snapshot_date = '2026-10-04'")
            out = await sched.run_pipeline(conn, "test_pipe_data", jobs, ctx)
            assert out["outcomes"]["apply"]["status"] == "succeeded"
            assert out["outcomes"]["kpi"]["status"] == "succeeded"
            # `ON CONFLICT DO NOTHING` (pipelines/snapshot.py): a day already
            # materialised is not rewritten, so the engine reports no id --
            # and that is the only case where it may report none.
            assert ((out["outcomes"]["kpi"]["run_id"] is not None)
                    == (before == 0))
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
    assert "--lookback-weeks" in f and "106" in f
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
                " VALUES ('2025-05-01', 'purchase', 777,"
                " '2025-05-02 00:00+00') RETURNING id")
            new = await conn.fetchval(
                "INSERT INTO segment_runs (as_of, mode, k, created_at)"
                " VALUES ('2025-06-01', 'purchase', 777,"
                " '2025-06-02 00:00+00') RETURNING id")
            ctx = _ctx()
            assert await sched._resolve_explain_run_id(
                conn, {"segment_run_id": str(old)}, ctx) == str(old)
            # scoped to purchase/k=777 (this test's own namespace, cleared by
            # _own_rows): newest of the two seeded rows.
            assert await sched._resolve_explain_run_id(
                conn, {"segment_run_id": "latest",
                       "segment_mode": "purchase",
                       "segment_k": 777}, ctx) == str(new)
            assert await sched._resolve_explain_run_id(
                conn, {"segment_mode": "purchase", "segment_k": 777},
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
                           "segment_k": 777}, scoped)
            # matrix_batch shares the resolver; engine name only labels
            # the failure message.
            assert await sched._resolve_segment_run_id(
                conn, {"segment_run_id": "latest",
                       "segment_mode": "purchase",
                       "segment_k": 777}, ctx, "matrix_batch") == str(new)
            with pytest.raises(sched.ManifestError,
                               match="matrix_batch: no segment_runs"):
                await sched._resolve_segment_run_id(
                    conn, {"segment_run_id": "latest",
                           "segment_mode": "purchase",
                           "segment_k": 777}, scoped, "matrix_batch")
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


@pytest.mark.asyncio
async def test_runner_labels_a_ticked_row(database_url: str, monkeypatch):
    """`upstream.tick` is the tick's only attribution.

    015 `pipeline_runs` has no actor column, so the loop puts one id per
    fire in `upstream`; a hand-run row keeps exactly `{"engine": ...}`
    (asserted above by test_runner_data_rows_carry_no_ml_revision).
    """
    monkeypatch.setitem(sched.ENGINE_BUILDERS, "fake_ok", _fake_ok)
    jobs = {"j1": {"engine": "fake_ok", "depends_on": [], "params": {}}}
    ctx = _ctx(pipeline="test_pipe_ticklabel", as_of=date(2026, 10, 7),
               tick="deadbeefcafe")
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            await sched.run_pipeline(conn, "test_pipe_ticklabel", jobs, ctx)
            raw = await conn.fetchval(
                "SELECT upstream FROM pipeline_runs"
                " WHERE pipeline = 'test_pipe_ticklabel' AND job = 'j1'")
    up = json.loads(raw) if isinstance(raw, str) else raw
    assert up["tick"] == "deadbeefcafe"
    assert up["engine"] == "fake_ok"


@pytest.mark.asyncio
async def test_live_manifest_rows_are_reachable_by_the_tick(database_url: str):
    """Every stored manifest has a minute that fires it under its own name.

    A schedule the loop cannot parse, or one that matches no minute in a
    week, is a pipeline that silently never runs -- and nothing else reads
    `pipeline_manifests` as a *schedule* (the app edits it, ml validates its
    shape). Brute-forces a week of business minutes per row (pure
    predicate, no YAML re-parse) and then asks the loop's own decision
    function about the first hit, so the row and the tick are compared
    through `due_pipelines`, not through a hand-copied cron string.
    """
    import yaml

    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sched.FETCH_MANIFESTS_SQL)
    if not rows:
        pytest.skip("no manifest rows in this database")
    start = datetime(2026, 10, 12, 0, 0, tzinfo=HK)      # a Monday
    week = [start + timedelta(minutes=m) for m in range(7 * 24 * 60)]
    for row in rows:
        pipeline, yaml_text = row["pipeline"], row["yaml"]
        spec = ((yaml.safe_load(yaml_text).get("pipelines") or {})
                .get(pipeline) or {}).get("schedule")
        assert spec, f"{pipeline}: stored manifest declares no schedule"
        hits = [w for w in week if sched.cron_matches(str(spec), w)]
        assert hits, f"{pipeline}: schedule {spec!r} never fires in a week"
        assert sched.due_pipelines(yaml_text, hits[0]) == [pipeline]


def test_once_logs_the_decision_and_exits_0_when_nothing_is_due(database_url):
    """The unattended path is the CLI, so `--once` is what a Task Scheduler
    entry loops: exit 0 when nothing is due, one JSON line carrying the
    decision, no engine spawned.
    """
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "run_pipeline.py"),
         "--once", "--now", "2026-10-17T02:00", "--db-url", database_url],
        cwd=REPO, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    line = json.loads(out.stdout.strip().splitlines()[-1])
    assert line["event"] == "once: done"
    assert line["fired"] == [] and line["failures"] == 0


def test_watch_interrupt_stops_the_loop(database_url, monkeypatch, capsys):
    """Ctrl-C inside the wait must stop the loop, log it, and leave 130 --
    a loop that swallows the interrupt is a process nothing can stop.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    import run_pipeline                                     # noqa: E402

    async def _interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(run_pipeline.asyncio, "sleep", _interrupt)
    monkeypatch.setattr(sys, "argv", [
        "run_pipeline.py", "--watch", "--interval", "1",
        "--now", "2026-10-17T02:00", "--db-url", database_url])
    with pytest.raises(SystemExit) as exc:
        run_pipeline.main()
    assert exc.value.code == 130
    printed = capsys.readouterr().out
    assert '"event": "pass"' in printed
    assert "interrupted" in printed

# ---- one cursor per job x as_of (review §4.3-3) ----
#
# `run_pipeline` used to upsert straight to status='running' and skip only on
# 'succeeded', so two ticks in the same cron minute both claimed the row and
# both executed the job. UPSERT_RUN_SQL now refuses a claim on a live
# 'running' row (and takes over one older than RUN_STALE_AFTER).

@pytest.mark.asyncio
async def test_claim_gate_lets_one_cursor_own_a_job(database_url: str):
    as_of = date(2026, 10, 4)
    async with _pool(database_url) as pool:
        conn_a = await pool.acquire()
        conn_b = await pool.acquire()
        try:
            first = await conn_a.fetchrow(
                sched.UPSERT_RUN_SQL, "test_pipe_claim", "j1", as_of,
                None, None, None)
            second = await conn_b.fetchrow(
                sched.UPSERT_RUN_SQL, "test_pipe_claim", "j1", as_of,
                None, None, None)
        finally:
            await pool.release(conn_a)
            await pool.release(conn_b)
    assert first is not None and first["id"]
    assert second is None, "a live 'running' row must refuse a second claim"


@pytest.mark.asyncio
async def test_claim_gate_takes_over_a_stale_running_row(database_url: str):
    as_of = date(2026, 10, 5)
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            await conn.fetchrow(sched.UPSERT_RUN_SQL, "test_pipe_stale",
                                "j1", as_of, None, None, None)
            await conn.execute(
                "UPDATE pipeline_runs SET started_at = now() - interval '3 hours'"
                " WHERE pipeline = 'test_pipe_stale' AND job = 'j1'")
            again = await conn.fetchrow(sched.UPSERT_RUN_SQL,
                                        "test_pipe_stale", "j1", as_of,
                                        None, None, None)
    assert again is not None, (
        "a crashed tick leaves status='running' forever; an abandoned row"
        " must be reclaimable after RUN_STALE_AFTER")


@pytest.mark.asyncio
async def test_runner_defers_job_another_cursor_is_running(
        database_url: str, monkeypatch):
    """The second cursor skips the job AND its dependants, without running."""
    ran: list[str] = []

    async def _engine(engine, params, conn, ctx):
        ran.append(engine)
        return None

    monkeypatch.setattr(sched, "_run_data_engine", _engine)
    jobs = {"j1": {"engine": "kpi_daily", "depends_on": [], "params": {}},
            "down": {"engine": "kpi_daily", "depends_on": ["j1"],
                     "params": {}}}
    as_of = date(2026, 10, 6)
    ctx = _ctx(pipeline="test_pipe_defer", as_of=as_of)
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            # another cursor claimed the row a moment ago
            await conn.fetchrow(sched.UPSERT_RUN_SQL, "test_pipe_defer",
                                "j1", as_of, None, None, None)
            out = await sched.run_pipeline(conn, "test_pipe_defer", jobs, ctx)
    assert out["outcomes"]["j1"]["status"] == "skipped"
    assert "already running" in out["outcomes"]["j1"]["error"]
    assert out["outcomes"]["down"]["status"] == "skipped"
    assert "upstream" in out["outcomes"]["down"]["error"]
    assert ran == [], "the deferred job must not execute at all"


@pytest.mark.asyncio
async def test_two_concurrent_cursors_execute_the_job_once(
        database_url: str, monkeypatch):
    """Two `--once` ticks at the same minute: one executes, one defers."""
    import asyncio

    ran: list[str] = []

    async def _engine(engine, params, conn, ctx):
        ran.append(engine)
        await asyncio.sleep(0.3)   # hold the claim while the peer tick runs
        return None

    monkeypatch.setattr(sched, "_run_data_engine", _engine)
    jobs = {"j1": {"engine": "kpi_daily", "depends_on": [], "params": {}}}
    as_of = date(2026, 10, 7)
    ctx = _ctx(pipeline="test_pipe_race", as_of=as_of)
    async with _pool(database_url) as pool:
        conn_a = await pool.acquire()
        conn_b = await pool.acquire()
        try:
            out_a, out_b = await asyncio.gather(
                sched.run_pipeline(conn_a, "test_pipe_race", jobs, ctx),
                sched.run_pipeline(conn_b, "test_pipe_race", jobs, ctx))
        finally:
            await pool.release(conn_a)
            await pool.release(conn_b)
    statuses = sorted(out["outcomes"]["j1"]["status"]
                      for out in (out_a, out_b))
    assert statuses == ["skipped", "succeeded"], statuses
    assert len(ran) == 1, f"job executed {len(ran)} times"


# ---- scope fan-out (the tick's per-brand batches) ----

async def _seed_brands(conn, n: int = 2):
    """Committed brand rows, because the tick reads them on its own
    connection (a test transaction would be invisible to it)."""
    gid = await conn.fetchval(
        "INSERT INTO groups (code, name)"
        " VALUES ('TICK-SCOPE', 'Tick Scope Test') RETURNING id")
    ids = []
    for i in range(n):
        ids.append(await conn.fetchval(
            "INSERT INTO brands (group_id, code, name)"
            " VALUES ($1, $2, $3) RETURNING id",
            gid, f"TICK-SCOPE-{i}", f"Tick Scope Brand {i}"))
    return gid, sorted(ids)


async def _drop_seeded_brands(conn, gid, ids) -> None:
    await conn.execute("DELETE FROM brands WHERE id = ANY($1::uuid[])", ids)
    await conn.execute("DELETE FROM groups WHERE id = $1", gid)


@pytest.mark.asyncio
async def test_tick_scopes_cover_every_brand_then_hq(database_url: str):
    """Scope is a run-level dimension, so "visible to a brand session" is
    a fan-out: one batch per brand in `brands`, then the (NULL, NULL) hq
    batch. A stamped row is invisible to hq and an hq row to a brand
    (stamp equality, fail closed), so dropping either half leaves a session
    class with no rows to read -- the 404 `no_snapshot` this fan-out is for.
    """
    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            gid, ids = await _seed_brands(conn, 2)
            try:
                scopes = await sched.tick_scopes(conn)
                all_brands = sorted(
                    r["id"] for r in await conn.fetch(sched.TICK_BRANDS_SQL))
            finally:
                await _drop_seeded_brands(conn, gid, ids)
    assert set(ids).issubset(set(all_brands))
    assert scopes[-1] == (None, None), "the hq batch must be last"
    assert scopes[:-1] == [(b, None) for b in all_brands]


async def _watch_fires(database_url, now, **kw) -> list[tuple]:
    """Run one `--once` pass with `run` recorded instead of executed."""
    sys.path.insert(0, str(REPO / "scripts"))
    import run_pipeline                                     # noqa: E402

    calls: list[tuple] = []

    async def _fake(pipeline, as_of, ml_repo, brand_id, group_id,
                    created_by, force, manifest_file, db_url, sync_run_id,
                    tick=None):
        calls.append((pipeline, str(brand_id), str(group_id)))
        return {"outcomes": {}}

    original = run_pipeline.run
    run_pipeline.run = _fake
    try:
        await run_pipeline.watch(60, REPO, kw.get("brand_id"),
                                 kw.get("group_id"), "test", False,
                                 database_url, True, now, kw.get("only"))
    finally:
        run_pipeline.run = original
    return calls


@pytest.mark.asyncio
async def test_watch_fires_one_batch_per_brand_plus_hq(
        database_url: str):
    """The unattended tick is the writer of the brand rows: one fire per
    brand, one for hq, all four inside the same window, or the analytics
    pages stay empty for every brand-scoped login (the only logins that
    exist on the demo deployment).
    """
    import yaml

    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sched.FETCH_MANIFESTS_SQL)
            if not rows:
                pytest.skip("no manifest rows in this database")
            pipeline, yaml_text = rows[0]["pipeline"], rows[0]["yaml"]
            spec = ((yaml.safe_load(yaml_text).get("pipelines") or {})
                    .get(pipeline) or {}).get("schedule")
            gid, ids = await _seed_brands(conn, 2)
            all_brands = sorted(
                r["id"] for r in await conn.fetch(sched.TICK_BRANDS_SQL))
    try:
        start = datetime(2026, 10, 12, 0, 0, tzinfo=HK)
        week = [start + timedelta(minutes=m) for m in range(7 * 24 * 60)]
        due = [w for w in week if sched.cron_matches(str(spec), w)][0]
        calls = await _watch_fires(database_url, due, only=[pipeline])
    finally:
        async with _pool(database_url) as pool:
            async with pool.acquire() as conn:
                await _drop_seeded_brands(conn, gid, ids)
    assert set(ids).issubset(set(all_brands)), "seeded brands must be seen"
    assert len(calls) == len(set(calls)), f"a scope fired twice: {calls}"
    assert set(calls) == ({(pipeline, str(b), "None") for b in all_brands}
                          | {(pipeline, "None", "None")}), calls


@pytest.mark.asyncio
async def test_watch_pinned_scope_does_not_fan_out(
        database_url: str):
    """A hand-run that pins a scope gets exactly that scope: the fan-out
    belongs to the unattended tick, and doubling a pinned run would write
    the same brand twice and cost every other brand a full pipeline.
    """
    import yaml

    async with _pool(database_url) as pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sched.FETCH_MANIFESTS_SQL)
            if not rows:
                pytest.skip("no manifest rows in this database")
            pipeline, yaml_text = rows[0]["pipeline"], rows[0]["yaml"]
            spec = ((yaml.safe_load(yaml_text).get("pipelines") or {})
                    .get(pipeline) or {}).get("schedule")
            gid, ids = await _seed_brands(conn, 2)
    pinned = ids[0]
    try:
        start = datetime(2026, 10, 12, 0, 0, tzinfo=HK)
        week = [start + timedelta(minutes=m) for m in range(7 * 24 * 60)]
        due = [w for w in week if sched.cron_matches(str(spec), w)][0]
        calls = await _watch_fires(database_url, due, only=[pipeline],
                                   brand_id=pinned)
    finally:
        async with _pool(database_url) as pool:
            async with pool.acquire() as conn:
                await _drop_seeded_brands(conn, gid, ids)
    assert calls == [(pipeline, str(pinned), "None")], calls
