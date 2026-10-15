"""Scheduled-pipeline runner (ml part 2, data-owned execution).

Reads a validated manifest (YAML: pipelines > jobs with engine /
depends_on / params / enabled) from `pipeline_manifests`, executes jobs
in topological order, and records each in `pipeline_runs` (re-runnable:
succeeded jobs are skipped unless `force`).

Execution model (zero import coupling to the ml repo):
- data-owned engines run in-process: `upload_apply` (apply all
  `validated` jobs' staged rows via pipelines.apply), `kpi_daily`
  (pipelines.snapshot upsert), `cohort_snapshot` (cohort retention
  upserts per granularity, idempotent per the 020 grain UNIQUE).
- ml-owned engines run as subprocesses rooted at the ml repo checkout
  with DATABASE_URL passed through. `churn_snapshot` / `clv_snapshot`
  argv are locked; `rfm_snapshot` / `segments` / `recs_batch` builders
  land once ml supplies their CLI argv (unknown engine -> job `failed`
  with an actionable error; downstream jobs `skipped`).

Full manifest validation belongs to ml (`ml_jobs/manifest.py`
validate()); the runner does structural parsing only and trusts stored
rows (the app config API validates at write).
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:  # pragma: no cover - import convenience
    sys.path.insert(0, str(REPO))

from pipelines import apply as ap  # noqa: E402
from pipelines import upload as up  # noqa: E402
from pipelines.snapshot import (  # noqa: E402
    COHORT_GRANULARITIES,
    COHORT_SNAPSHOT_UPSERT_SQL,
    KPI_DAILY_UPSERT_SQL,
)

# Data-owned engines run in-process (ml engines go through argv builders).
DATA_ENGINES = ("upload_apply", "kpi_daily", "cohort_snapshot")

# Default retention offsets per granularity for the cohort_snapshot engine
# (overridable per job via params.max_offsets).
COHORT_DEFAULT_MAX_OFFSETS = {"week": 12, "month": 12, "year": 5}

FETCH_MANIFEST_SQL = "SELECT yaml FROM pipeline_manifests WHERE pipeline = $1"

UPSERT_RUN_SQL = """
INSERT INTO pipeline_runs
  (pipeline, job, as_of, status, started_at, brand_id, group_id)
VALUES ($1, $2, $3::date, 'running', now(), $4, $5)
ON CONFLICT (pipeline, job, as_of,
  COALESCE(brand_id, '00000000-0000-0000-0000-000000000000'::uuid),
  COALESCE(group_id, '00000000-0000-0000-0000-000000000000'::uuid))
DO UPDATE SET status = 'running', started_at = now(), error = NULL,
  finished_at = NULL
RETURNING id
"""

FINISH_RUN_SQL = """
UPDATE pipeline_runs
SET status = $2, run_id = $3, upstream = $4::jsonb, error = $5,
  finished_at = now()
WHERE id = $1
"""

FETCH_RUN_SQL = """
SELECT status FROM pipeline_runs
WHERE pipeline = $1 AND job = $2 AND as_of = $3::date
  AND brand_id IS NOT DISTINCT FROM $4::uuid
  AND group_id IS NOT DISTINCT FROM $5::uuid
"""

VALIDATED_JOBS_SQL = """
SELECT id, source FROM upload_jobs WHERE status = 'validated'
  AND brand_id IS NOT DISTINCT FROM $1::uuid
  AND group_id IS NOT DISTINCT FROM $2::uuid
ORDER BY created_at
"""


class ManifestError(ValueError):
    """Structural manifest problem (ml owns semantic validation)."""


class UnknownEngineError(LookupError):
    """No argv builder for an ml engine (awaiting ml CLI contract)."""


@dataclass
class JobCtx:
    pipeline: str
    as_of: date
    brand_id: Any
    group_id: Any
    created_by: str | None
    ml_repo: Path | None = None
    db_url: str | None = None
    timeout_s: float = 1800.0
    extra_env: dict = field(default_factory=dict)


def parse_manifest(yaml_text: str) -> dict[str, dict[str, dict]]:
    """Structural parse: {pipeline: {job: {engine, depends_on, params}}}."""
    import yaml

    try:
        doc = yaml.safe_load(yaml_text)
    except Exception as exc:
        raise ManifestError(f"manifest is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict) or "pipelines" not in doc:
        raise ManifestError("manifest needs a top-level 'pipelines' map")
    out: dict[str, dict[str, dict]] = {}
    for pname, pdef in doc["pipelines"].items():
        if not isinstance(pdef, dict) or not isinstance(
                pdef.get("jobs"), dict):
            raise ManifestError(f"pipeline {pname!r} needs a 'jobs' map")
        jobs: dict[str, dict] = {}
        for jname, jdef in pdef["jobs"].items():
            if not isinstance(jdef, dict) or "engine" not in jdef:
                raise ManifestError(
                    f"job {pname}.{jname} needs an 'engine'")
            if jdef.get("enabled", True) is False:
                continue
            deps = jdef.get("depends_on") or []
            if not isinstance(deps, list):
                raise ManifestError(
                    f"job {pname}.{jname} depends_on must be a list")
            jobs[jname] = {"engine": jdef["engine"],
                           "depends_on": deps,
                           "params": jdef.get("params") or {}}
        out[pname] = jobs
    return out


def topo_sort(jobs: dict[str, dict]) -> list[str]:
    """Kahn's algorithm over depends_on; raises ManifestError on cycles."""
    deps = {name: set(j["depends_on"]) for name, j in jobs.items()}
    for name, ds in deps.items():
        for d in ds:
            if d not in jobs:
                raise ManifestError(
                    f"job {name!r} depends on unknown job {d!r}")
    order: list[str] = []
    ready = sorted(n for n, ds in deps.items() if not ds)
    while ready:
        name = ready.pop(0)
        order.append(name)
        for other, ds in deps.items():
            if name in ds:
                ds.discard(name)
                if not ds and other not in order and other not in ready:
                    ready.append(other)
        ready.sort()
    if len(order) != len(jobs):
        stuck = sorted(set(jobs) - set(order))
        raise ManifestError(f"dependency cycle involving {stuck}")
    return order


def _scope_args(ctx: JobCtx) -> list[str]:
    args: list[str] = []
    if ctx.brand_id is not None:
        args += ["--brand-id", str(ctx.brand_id)]
    if ctx.group_id is not None:
        args += ["--group-id", str(ctx.group_id)]
    if ctx.created_by:
        args += ["--created-by", ctx.created_by]
    return args


def _ml_module(module: str) -> Callable[[dict, JobCtx], list[str]]:
    """Argv builder for ml CLIs: `python -m <module> DB_URL --flags...`.

    Conventions (ml contract): positional DATABASE_URL, stdout = produced
    run/batch id, exit 0 on success; --brand-id/--group-id UUID-or-omitted;
    --created-by text. Engine params come from the manifest job's `params`
    (snake_case keys accepted, e.g. horizon_days / top_n / min_support).
    """
    def build(params: dict, ctx: JobCtx) -> list[str]:
        if ctx.ml_repo is None:
            raise UnknownEngineError(
                "ml job needs --ml-repo (path to the ml checkout)")
        if not ctx.db_url:
            raise UnknownEngineError(
                "ml job needs JobCtx.db_url (DATABASE_URL to pass)")
        return [sys.executable, "-m", module, ctx.db_url]
    return build


def _with_common(argv: list[str], params: dict, ctx: JobCtx,
                 with_as_of: bool = True,
                 with_scope: bool = True) -> list[str]:
    # with_scope=False is for CLIs that reject --brand-id/--group-id
    # (explain_batch: scope rides the segment run; only --created-by
    # is passed). Sending a flag the CLI doesn't accept = argparse
    # exit 2 on every scoped tick — strip, don't hope.
    if with_as_of:
        argv += ["--as-of", ctx.as_of.isoformat()]
    if with_scope:
        argv += _scope_args(ctx)
    elif ctx.created_by:
        argv += ["--created-by", ctx.created_by]
    return argv


def _p(params: dict, *names: str, default: Any = None) -> Any:
    for n in names:
        if n in params and params[n] is not None:
            return params[n]
    return default


def _build_rfm(params: dict, ctx: JobCtx) -> list[str]:
    argv = _ml_module("ml_jobs.run_snapshot")(params, ctx)
    argv += ["--mode", str(_p(params, "mode", default="rolling_365"))]
    if _p(params, "date_from", "date-from"):
        argv += ["--date-from", str(_p(params, "date_from", "date-from"))]
    if _p(params, "date_to", "date-to"):
        argv += ["--date-to", str(_p(params, "date_to", "date-to"))]
    return _with_common(argv, params, ctx)


def _build_churn(params: dict, ctx: JobCtx) -> list[str]:
    argv = _ml_module("ml_jobs.run_churn_snapshot")(params, ctx)
    argv += ["--activity", str(_p(params, "activity", default="orders")),
             "--horizon-days",
             str(_p(params, "horizon_days", "horizon-days", default=90)),
             "--model", str(_p(params, "model", default="bgnbd"))]
    return _with_common(argv, params, ctx)


def _build_clv(params: dict, ctx: JobCtx) -> list[str]:
    argv = _ml_module("ml_jobs.run_clv_snapshot")(params, ctx)
    argv += ["--horizon-days",
             str(_p(params, "horizon_days", "horizon-days", default=90)),
             "--model", str(_p(params, "model", default="bgnbd"))]
    return _with_common(argv, params, ctx)


def _build_recs(params: dict, ctx: JobCtx) -> list[str]:
    argv = _ml_module("ml_jobs.run_recs_batch")(params, ctx)
    argv += ["--top-n", str(_p(params, "top_n", "top-n", default=10)),
             "--min-support",
             str(_p(params, "min_support", "min-support", default=2)),
             "--min-confidence",
             str(_p(params, "min_confidence", "min-confidence",
                     default=0.2)),
             "--content",
             str(_p(params, "content", default="off"))]
    if _p(params, "include_user_cf", "include-user-cf", default=False):
        argv += ["--include-user-cf"]
    # recs_batch takes no --as-of; the tick date still scopes pipeline_runs.
    return _with_common(argv, params, ctx, with_as_of=False)


def _build_segments(params: dict, ctx: JobCtx) -> list[str]:
    # CONFIRMED verbatim (ml): run_segments_snapshot DB_URL
    # --mode {purchase|overall|behavior} --k --seed [--as-of] + scope;
    # --algorithm {kmeans|gmm|agglomerative|dbscan|hdbscan} (default kmeans).
    # Values pass through; ml validates against the registry/dispatcher.
    argv = _ml_module("ml_jobs.run_segments_snapshot")(params, ctx)
    argv += ["--mode", str(_p(params, "mode", default="purchase")),
             "--k", str(_p(params, "k", default=6)),
             "--seed", str(_p(params, "seed", default=0)),
             "--algorithm",
             str(_p(params, "algorithm", default="kmeans"))]
    return _with_common(argv, params, ctx)


def _build_forecast(params: dict, ctx: JobCtx) -> list[str]:
    # CONFIRMED verbatim (ml): run_forecast_snapshot DB_URL
    # --horizon-days --lookback-weeks --min-history-weeks [--as-of]
    # + scope/created-by. Defaults mirror the ml CLI (28d / 52wks / 8wks).
    argv = _ml_module("ml_jobs.run_forecast_snapshot")(params, ctx)
    argv += ["--horizon-days",
             str(_p(params, "horizon_days", "horizon-days", default=28)),
             "--lookback-weeks",
             str(_p(params, "lookback_weeks", "lookback-weeks",
                     default=52)),
             "--min-history-weeks",
             str(_p(params, "min_history_weeks", "min-history-weeks",
                     default=8))]
    return _with_common(argv, params, ctx)


def _build_assoc(params: dict, ctx: JobCtx) -> list[str]:
    # CONFIRMED verbatim (ml): run_assoc_batch DB_URL
    # --min-support --min-confidence --min-rules + scope/created-by.
    # Defaults mirror the ml CLI (5 / 0.2 / 50).
    argv = _ml_module("ml_jobs.run_assoc_batch")(params, ctx)
    argv += ["--min-support",
             str(_p(params, "min_support", "min-support", default=5)),
             "--min-confidence",
             str(_p(params, "min_confidence", "min-confidence",
                     default=0.2)),
             "--min-rules",
             str(_p(params, "min_rules", "min-rules", default=50))]
    # assoc_batch takes no --as-of; the tick date still scopes pipeline_runs.
    return _with_common(argv, params, ctx, with_as_of=False)


def _build_explain(params: dict, ctx: JobCtx) -> list[str]:
    # CONFIRMED verbatim (ml): run_explain_batch DB_URL
    # --segment-run-id (required) --top-n + created-by. Note: explain
    # takes NO scope flags — scope rides the resolved segment run.
    # run_pipeline resolves "latest"/missing/zero segment_run_id to the
    # newest segment run for (segment_mode, [segment_k], scope) before
    # dispatch, so manifests never store a stale fixed id.
    sid = params.get("segment_run_id")
    if not sid:
        raise ManifestError(
            "explain_batch needs params.segment_run_id "
            "(concrete UUID or 'latest')")
    argv = _ml_module("ml_jobs.run_explain_batch")(params, ctx)
    argv += ["--segment-run-id", str(sid),
             "--top-n", str(_p(params, "top_n", "top-n", default=20))]
    # explain_batch takes no --as-of AND no scope flags (argparse
    # rejects them); scope rides the resolved segment run.
    return _with_common(argv, params, ctx, with_as_of=False,
                        with_scope=False)


ENGINE_BUILDERS: dict[str, Callable[[dict, JobCtx], list[str]]] = {
    "rfm_snapshot": _build_rfm,
    "churn_snapshot": _build_churn,
    "clv_snapshot": _build_clv,
    "recs_batch": _build_recs,
    "segments": _build_segments,  # argv per ml stated form; confirm pending
    "forecast_snapshot": _build_forecast,
    "assoc_batch": _build_assoc,
    "explain_batch": _build_explain,
}


async def _resolve_explain_run_id(conn, params: dict,
                                   ctx: JobCtx) -> str:
    """Resolve an explain_batch segment_run_id to a concrete UUID.

    Pass-through for concrete UUIDs. "latest" (or a missing/zero id)
    resolves to the newest segment run for (segment_mode, [segment_k],
    scope); no such run -> ManifestError with an actionable message.
    """
    sid = params.get("segment_run_id")
    if sid and sid not in ("latest",
                          "00000000-0000-0000-0000-000000000000"):
        return str(sid)
    mode = str(params.get("segment_mode", "overall"))
    seg_k = params.get("segment_k")
    if seg_k is None:
        row = await conn.fetchrow(
            "SELECT id FROM segment_runs WHERE mode = $1"
            " AND brand_id IS NOT DISTINCT FROM $2::uuid"
            " AND group_id IS NOT DISTINCT FROM $3::uuid"
            " ORDER BY created_at DESC LIMIT 1",
            mode, ctx.brand_id, ctx.group_id)
    else:
        row = await conn.fetchrow(
            "SELECT id FROM segment_runs WHERE mode = $1 AND k = $2"
            " AND brand_id IS NOT DISTINCT FROM $3::uuid"
            " AND group_id IS NOT DISTINCT FROM $4::uuid"
            " ORDER BY created_at DESC LIMIT 1",
            mode, int(seg_k), ctx.brand_id, ctx.group_id)
    if row is None:
        raise ManifestError(
            "explain_batch: no segment_runs row for"
            f" mode={mode!r} k={seg_k!r} in scope"
            " (run segments first)")
    return str(row["id"])


def _parse_run_id(output: str) -> uuid.UUID | None:
    for token in reversed(output.strip().split()):
        try:
            return uuid.UUID(token.strip())
        except ValueError:
            continue
    return None


def _run_sync(argv: list[str], cwd: Path, env: dict,
              timeout_s: float) -> str:
    import subprocess

    try:
        cp = subprocess.run(argv, cwd=str(cwd), env=env,
                            capture_output=True, text=True,
                            timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"job timed out after {timeout_s}s") from exc
    text = (cp.stdout or "") + (cp.stderr or "")
    if cp.returncode != 0:
        raise RuntimeError(f"exit {cp.returncode}: {text[-2000:]}")
    return text


async def _run_subprocess(argv: list[str], cwd: Path, ctx: JobCtx) -> str:
    # Thread-based (not asyncio subprocess): works on any event loop,
    # including Windows SelectorEventLoop which lacks subprocess support.
    env = dict(os.environ)
    env.update(ctx.extra_env)
    return await asyncio.to_thread(_run_sync, argv, cwd, env,
                                    ctx.timeout_s)


async def apply_pending(conn, ctx: JobCtx) -> dict[str, Any]:
    """`upload_apply`: apply every validated job's staged rows (data-owned).

    Each upload job keeps its own counts/invariant; this just sweeps the
    backlog a scheduler tick finds. Returns per-upload-job outcomes.
    """
    import json

    swept: list[dict[str, Any]] = []
    jobs = await conn.fetch(VALIDATED_JOBS_SQL, ctx.brand_id, ctx.group_id)
    for rec in jobs:
        jid, source = rec["id"], rec["source"]
        kind = await conn.fetchval(
            "SELECT kind FROM upload_jobs WHERE id = $1", jid)
        async with conn.transaction():
            res = await ap.apply_job(conn, str(jid), kind, source)
        # lifetime counts: previously committed ok-rows split into
        # applied + skipped + newly-failed; quarantined grows by fails.
        prev_q = await conn.fetchval(
            "SELECT quarantined_rows FROM upload_jobs WHERE id = $1", jid)
        committed = res["applied"] + res["skipped"]
        quarantined = int(prev_q or 0) + len(res["failed"])
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM staging_rows WHERE upload_job_id = $1",
            jid)
        up.reconcile(int(total), committed, quarantined)
        status = ("committed" if not res["failed"]
                  else "committed_with_rejects")
        await conn.execute(up.FINISH_JOB_SQL, jid, status, int(total),
                           int(total), committed, quarantined, None)
        swept.append({"upload_job_id": str(jid), "status": status,
                      "applied": res["applied"],
                      "skipped": res["skipped"],
                      "failed_rows": [f["row_number"]
                                      for f in res["failed"]]})
    return {"swept": len(swept), "jobs": swept}


async def _run_data_engine(engine: str, params: dict,
                           conn, ctx: JobCtx) -> uuid.UUID | None:
    if engine == "upload_apply":
        strategy = params.get("strategy", "validated_only")
        if strategy != "validated_only":
            raise ManifestError(
                f"upload_apply strategy {strategy!r} unsupported "
                f"(only 'validated_only')")
        await apply_pending(conn, ctx)
        return None
    if engine == "kpi_daily":
        row = await conn.fetchrow(KPI_DAILY_UPSERT_SQL, ctx.as_of,
                                  ctx.brand_id, ctx.group_id)
        return row["id"] if row else None
    if engine == "cohort_snapshot":
        granularities = params.get("granularities",
                                   list(COHORT_GRANULARITIES))
        unknown = [g for g in granularities
                   if g not in COHORT_GRANULARITIES]
        if unknown:
            raise ManifestError(
                f"cohort_snapshot granularities {unknown!r} invalid "
                f"(expected subset of {list(COHORT_GRANULARITIES)})")
        max_offsets = params.get("max_offsets", {}) or {}
        for gran in granularities:
            max_off = int(max_offsets.get(
                gran, COHORT_DEFAULT_MAX_OFFSETS[gran]))
            if max_off < 0:
                raise ManifestError(
                    f"cohort_snapshot max_offset {max_off!r} for "
                    f"{gran!r} must be >= 0")
            await conn.execute(COHORT_SNAPSHOT_UPSERT_SQL, gran, max_off,
                               ctx.brand_id, ctx.group_id)
        return None
    raise UnknownEngineError(f"unknown data engine {engine!r}")


async def run_pipeline(conn, pipeline: str, jobs: dict[str, dict],
                       ctx: JobCtx, force: bool = False) -> dict[str, Any]:
    """Execute one pipeline's jobs in topo order, recording pipeline_runs."""
    import json

    order = topo_sort(jobs)
    outcomes: dict[str, dict[str, Any]] = {}
    for name in order:
        job = jobs[name]
        engine = job["engine"]
        failed_upstream = [d for d in job["depends_on"]
                           if outcomes.get(d, {}).get("status")
                           not in ("succeeded", "skipped")]
        if failed_upstream:
            outcomes[name] = {"status": "skipped",
                              "error": f"upstream failed: {failed_upstream}"}
            continue
        if not force:
            prev = await conn.fetchrow(
                FETCH_RUN_SQL, pipeline, name, ctx.as_of,
                ctx.brand_id, ctx.group_id)
            if prev is not None and prev["status"] == "succeeded":
                outcomes[name] = {"status": "skipped",
                                  "error": None, "note": "already succeeded"}
                continue
        run_row = await conn.fetchrow(
            UPSERT_RUN_SQL, pipeline, name, ctx.as_of,
            ctx.brand_id, ctx.group_id)
        rid = run_row["id"]
        try:
            if engine in DATA_ENGINES:
                run_id = await _run_data_engine(engine, job["params"],
                                                conn, ctx)
                output_note = ""
            elif engine == "explain_batch":
                job_params = dict(job["params"])
                job_params["segment_run_id"] = \
                    await _resolve_explain_run_id(conn, job_params, ctx)
                argv = ENGINE_BUILDERS[engine](job_params, ctx)
                assert ctx.ml_repo is not None
                output_note = await _run_subprocess(argv, ctx.ml_repo, ctx)
                run_id = _parse_run_id(output_note)
            elif engine in ENGINE_BUILDERS:
                argv = ENGINE_BUILDERS[engine](job["params"], ctx)
                assert ctx.ml_repo is not None
                output_note = await _run_subprocess(argv, ctx.ml_repo, ctx)
                run_id = _parse_run_id(output_note)
            else:
                raise UnknownEngineError(
                    f"no argv builder for engine {engine!r} "
                    f"(ml CLI contract pending)")
            await conn.execute(FINISH_RUN_SQL, rid, "succeeded", run_id,
                               json.dumps({"engine": engine}), None)
            outcomes[name] = {"status": "succeeded", "run_id": run_id,
                              "note": output_note[-500:]
                              if engine not in DATA_ENGINES
                              else None}
        except Exception as exc:  # noqa: BLE001 - recorded on the run row
            await conn.execute(FINISH_RUN_SQL, rid, "failed", None,
                               json.dumps({"engine": engine}),
                               f"{type(exc).__name__}: {exc}"[:2000])
            outcomes[name] = {"status": "failed",
                              "error": f"{type(exc).__name__}: {exc}"[:500]}
    return {"pipeline": pipeline, "as_of": ctx.as_of.isoformat(),
            "outcomes": outcomes}
