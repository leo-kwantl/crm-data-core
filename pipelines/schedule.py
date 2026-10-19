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
- RFM has TWO writers with different rulesets, so the engine name decides
  the stored rows: `rfm_snapshot` = ml `run_snapshot` -> `store_rfm`,
  rules_version 'v1', single window, APPEND-only (no ON CONFLICT).
  `ld8_snapshot` = ml `run_ld8_snapshot` -> `store_ld8`, rules_version
  'v2', four fixed windows (current|mom|yoy|lifetime), find-or-create.
  v2 is canonical for time-aware rows (031): the daily capture belongs on
  `ld8_snapshot` — see contracts.md 'Daily RFM writer'.
- `explain_batch` / `matrix_batch` take a RESOLVED `segment_run_id`
  (newest segment run for the scope) instead of scope flags: their
  CLIs reject --brand-id/--group-id and scope rides the segment run.

Full manifest validation belongs to ml (`ml_jobs/manifest.py`
validate()); the runner does structural parsing only and trusts stored
rows (the app config API validates at write).

Nothing in the product fires a tick by itself: `cron_matches` /
`due_pipelines` are the pure "is this manifest due at <business
wall-clock>" decision, and `scripts/run_pipeline.py --watch` is the
in-repo loop that uses them (cron may drive the same entrypoint
one-shot instead). Idempotence stays the runner's, never the loop's:
the window key is (pipeline, job, as_of, scope) and a succeeded row
skips, so a second tick for a window writes nothing new.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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

# A 'running' row older than this is abandoned, not in flight: a crashed or
# killed tick leaves status='running' forever, and the claim gate below would
# then block that job until someone edited the row by hand. Two hours is
# longer than any single job may run (JobCtx.timeout_s defaults to 30 min).
RUN_STALE_AFTER = "interval '2 hours'"

# Default retention offsets per granularity for the cohort_snapshot engine
# (overridable per job via params.max_offsets).
COHORT_DEFAULT_MAX_OFFSETS = {"week": 12, "month": 12, "year": 5}

FETCH_MANIFEST_SQL = "SELECT yaml FROM pipeline_manifests WHERE pipeline = $1"
FETCH_MANIFESTS_SQL = ("SELECT pipeline, yaml FROM pipeline_manifests"
                       " ORDER BY pipeline")

# The tick's scope fan-out. Scope is a run-level dimension: `--brand-id`
# reaches every job of one invocation, so per-brand rows cost one invocation
# per brand -- and a stamped row is invisible to hq and vice versa
# (contracts.md "Scope is a run-level dimension"), which is why the hq
# (NULL, NULL) batch has to be fired too instead of being replaced by the
# brand ones. `brands` is the live registry: a brand a session can be scoped
# to must be able to see its own snapshots.
TICK_BRANDS_SQL = "SELECT id FROM brands ORDER BY id"


async def tick_scopes(conn) -> list[tuple[Any, Any]]:
    """The scopes one unattended tick covers: every brand, then hq.

    A hand-run that pins `--brand-id`/`--group-id` does not come through
    here -- one pinned scope is what the caller asked for. An empty
    `brands` table yields just hq, so a fresh database still ticks.
    """
    brands = [r["id"] for r in await conn.fetch(TICK_BRANDS_SQL)]
    return [(b, None) for b in brands] + [(None, None)]

UPSERT_RUN_SQL = f"""
INSERT INTO pipeline_runs
  (pipeline, job, as_of, status, started_at, brand_id, group_id,
   sync_run_id)
VALUES ($1, $2, $3::date, 'running', now(), $4, $5, $6::uuid)
ON CONFLICT (pipeline, job, as_of,
  COALESCE(brand_id, '00000000-0000-0000-0000-000000000000'::uuid),
  COALESCE(group_id, '00000000-0000-0000-0000-000000000000'::uuid))
DO UPDATE SET status = 'running', started_at = now(), error = NULL,
  finished_at = NULL,
  sync_run_id = COALESCE(pipeline_runs.sync_run_id, EXCLUDED.sync_run_id)
WHERE pipeline_runs.status <> 'running'
   OR pipeline_runs.started_at < now() - {RUN_STALE_AFTER}
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


# --- Date-windowing vocabulary (LD9, additive-only) ---
#
# Named windows for scheduler job params, stolen vocabulary from
# legacy's get_processed_date_filter (rolling/past/current) with the
# end date resolved through LD5 as-of modes (latest/current). NOT
# stolen: legacy's eval(relativedelta) implementation.
#
#   rolling(n, unit): last n units ending at end (guard: <30d rejected).
#   past(n, unit):    n complete units before the unit containing end.
#   current(unit):    start of end's unit through end (to-date).
# Units: days | weeks (Mon-Sun) | months (calendar).
WINDOW_MODES = ("rolling", "past", "current")
WINDOW_UNITS = ("days", "weeks", "months")
ROLLING_MIN_DAYS = 30


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    year, m = d.year + m // 12, m % 12 + 1
    import calendar
    last = calendar.monthrange(year, m)[1]
    return date(year, m, min(d.day, last))


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def resolve_window(mode: str, n: int | None, unit: str,
                   end: date) -> tuple[date, date]:
    """Resolve (date_from, date_to) for a named scheduler window.

    Raises ValueError on unknown mode/unit, bad n, or the rolling
    guard (rolling windows under 30 days are rejected: too noisy to
    schedule against).
    """
    if mode not in WINDOW_MODES:
        raise ValueError(f"unknown window mode: {mode!r} "
                         f"(expected one of {WINDOW_MODES})")
    if unit not in WINDOW_UNITS:
        raise ValueError(f"unknown window unit: {unit!r} "
                         f"(expected one of {WINDOW_UNITS})")
    if mode in ("rolling", "past"):
        if not isinstance(n, int) or n < 1:
            raise ValueError(f"{mode} window needs n >= 1, got {n!r}")
    if mode == "rolling":
        days = {"days": 1, "weeks": 7, "months": 30}[unit] * n
        if days < ROLLING_MIN_DAYS:
            raise ValueError(
                f"rolling window {n} {unit} (~{days}d) below minimum "
                f"{ROLLING_MIN_DAYS}d")
        if unit == "days":
            return end - timedelta(days=n - 1), end
        if unit == "weeks":
            return end - timedelta(weeks=n) + timedelta(days=1), end
        return _add_months(end, -(n - 1)).replace(day=1), end
    if mode == "past":
        if unit == "days":
            return end - timedelta(days=n), end - timedelta(days=1)
        if unit == "weeks":
            monday = end - timedelta(days=end.weekday())
            return (monday - timedelta(weeks=n),
                    monday - timedelta(days=1))
        first = _month_start(end)
        return _add_months(first, -n), first - timedelta(days=1)
    # current: start of end's unit through end
    if unit == "days":
        return end, end
    if unit == "weeks":
        return end - timedelta(days=end.weekday()), end
    return _month_start(end), end


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
    sync_run_id: str | None = None  # LD3: feeding 019 connector_sync_runs row
    tick: str | None = None  # unattended loop's fire id; hand-runs leave it None
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


def _cron_field(spec: str, lo: int, hi: int, name: str) -> set[int]:
    """One cron field -> the values it matches.

    Supported: `*`, `n`, `a-b`, `*/n`, `a-b/n` and comma lists of those.
    Names (`mon`) and shortcuts (`@daily`) are rejected rather than
    guessed: the stored manifests are numbers, and silently reading
    `mon` as "never" would turn a typo into a pipeline that never runs.
    """
    values: set[int] = set()
    for raw in spec.split(","):
        part = raw.strip()
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) < 1:
                raise ManifestError(
                    f"schedule {name} step {spec!r} must be */n with n >= 1")
            step = int(raw_step)
        if part == "*":
            start, end = lo, hi
        elif "-" in part[1:]:   # `-3` is not a range, `1-3` is
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise ManifestError(
                    f"schedule {name} range {spec!r} is not a-b")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise ManifestError(
                f"schedule {name} {spec!r} must be *, n, a-b, */n or a list"
                " (names and @shortcuts are not supported)")
        if not (lo <= start <= end <= hi):
            raise ManifestError(
                f"schedule {name} {spec!r} is outside {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return values


def cron_matches(expr: str, when: datetime) -> bool:
    """5-field cron (min hour dom month dow) against a wall-clock `when`.

    Day-of-week is 0 = Sunday, so `datetime.weekday()` shifts by one;
    7 is accepted as Sunday too. When BOTH day fields are restricted the
    classic Vixie rule applies -- the day matches if EITHER does -- so
    `0 2 * * 1` is "Monday 02:00" and `0 2 1 * *` is "the 1st, 02:00"
    (a manifest needing both must express it in a job param, not here).

    `when` is taken as given: the caller supplies the *business* clock
    (`business_now()`, Asia/Hong_Kong), never the host's, so which window
    is due does not depend on where the process happens to run.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise ManifestError(
            f"schedule {expr!r} must have 5 fields"
            " (min hour day-of-month month day-of-week)")
    minutes = _cron_field(fields[0], 0, 59, "minute")
    hours = _cron_field(fields[1], 0, 23, "hour")
    doms = _cron_field(fields[2], 1, 31, "day-of-month")
    months = _cron_field(fields[3], 1, 12, "month")
    dows = _cron_field(fields[4], 0, 7, "day-of-week")
    if 7 in dows:
        dows = (dows - {7}) | {0}
    if (when.minute not in minutes or when.hour not in hours
            or when.month not in months):
        return False
    dom_any = fields[2] == "*"
    dow_any = fields[4] == "*"
    dom_ok = when.day in doms
    dow_ok = ((when.weekday() + 1) % 7) in dows
    if dom_any:
        return dow_ok if not dow_any else True
    if dow_any:
        return dom_ok
    return dom_ok or dow_ok


def due_pipelines(yaml_text: str, when: datetime) -> list[str]:
    """Names inside ONE manifest whose declared schedule fires at `when`.

    A pipeline with no `schedule` is manual-only: the tick must not
    invent a cadence for it. A malformed one raises `ManifestError` for
    that manifest alone -- the loop logs it and keeps ticking rather
    than letting one bad row stop the schedule. The caller runs the row
    `pipeline_manifests.pipeline` key; a YAML whose inner name differs
    from it fails loudly in `run_pipeline` ("defines no jobs").
    """
    import yaml

    try:
        doc = yaml.safe_load(yaml_text)
    except Exception as exc:
        raise ManifestError(f"manifest is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise ManifestError("manifest needs a top-level 'pipelines' map")
    out: list[str] = []
    for pname, pdef in (doc.get("pipelines") or {}).items():
        spec = (pdef or {}).get("schedule") if isinstance(pdef, dict) else None
        if not spec:
            continue
        if cron_matches(str(spec), when):
            out.append(str(pname))
    return sorted(out)


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
    # LEGACY v1 writer (rules_version 'v1', single window, append-only):
    # every tick appends a NEW row for the same window_key, so a daily job
    # on this engine makes the two newest 'current' rows cross-version by
    # construction (the v2 find is v2-scoped and cannot supersede them).
    # The daily capture belongs on `ld8_snapshot`; this stays for v1
    # history/backfill only. See contracts.md 'Daily RFM writer'.
    argv = _ml_module("ml_jobs.run_snapshot")(params, ctx)
    argv += ["--mode", str(_p(params, "mode", default="rolling_365"))]
    if _p(params, "date_from", "date-from"):
        argv += ["--date-from", str(_p(params, "date_from", "date-from"))]
    if _p(params, "date_to", "date-to"):
        argv += ["--date-to", str(_p(params, "date_to", "date-to"))]
    return _with_common(argv, params, ctx)


def _build_ld8(params: dict, ctx: JobCtx) -> list[str]:
    # CONFIRMED verbatim (ml): run_ld8_snapshot DB_URL [--as-of]
    # --brand-id/--group-id --created-by. One run writes FOUR window
    # snapshots (current = rolling 365d ending as-of | mom | yoy |
    # lifetime), all rules_version 'v2', plus the sizes/movement rows.
    # The CLI declares no --mode/--date-from/--date-to: a manifest still
    # carrying the legacy single-window params would be an argparse exit 2
    # on every tick, so reject it HERE with the fix named instead of
    # letting the subprocess fail opaquely.
    for legacy in ("mode", "date_from", "date-from", "date_to", "date-to"):
        if params.get(legacy) is not None:
            raise ManifestError(
                f"ld8_snapshot computes all four fixed windows "
                f"(current|mom|yoy|lifetime) in one run; drop "
                f"params.{legacy} (its CLI takes only --as-of + scope)")
    argv = _ml_module("ml_jobs.run_ld8_snapshot")(params, ctx)
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
             # Default mirrors serving's read default (k=5): the read is
             # per (mode, k, scope) over the latest run, so an omitted
             # param must not diverge from what the product reads.
             # Manifests still pin k explicitly (015 seeds k: 5).
             "--k", str(_p(params, "k", default=5)),
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


def _build_matrix(params: dict, ctx: JobCtx) -> list[str]:
    # CONFIRMED verbatim (ml, run_matrix_batch.py:33-38):
    # run_matrix_batch DB_URL --segment-run-id (required). Argparse
    # accepts NOTHING else — no --as-of, no scope flags, and unlike
    # explain no --created-by — so pass only the resolved id (a flag
    # the CLI does not declare = exit 2 on every tick). Writes BOTH
    # layouts (029 wide + 032 long) for the one run.
    sid = params.get("segment_run_id")
    if not sid:
        raise ManifestError(
            "matrix_batch needs params.segment_run_id "
            "(concrete UUID or 'latest')")
    argv = _ml_module("ml_jobs.run_matrix_batch")(params, ctx)
    argv += ["--segment-run-id", str(sid)]
    return argv


ENGINE_BUILDERS: dict[str, Callable[[dict, JobCtx], list[str]]] = {
    # rfm_snapshot = legacy v1 writer; ld8_snapshot = canonical v2 writer
    # (four time-aware windows). Pick by ruleset, not by habit.
    "rfm_snapshot": _build_rfm,
    "ld8_snapshot": _build_ld8,
    "churn_snapshot": _build_churn,
    "clv_snapshot": _build_clv,
    "recs_batch": _build_recs,
    "segments": _build_segments,  # argv per ml stated form; confirm pending
    "forecast_snapshot": _build_forecast,
    "assoc_batch": _build_assoc,
    "explain_batch": _build_explain,
    "matrix_batch": _build_matrix,
}


async def _resolve_segment_run_id(conn, params: dict, ctx: JobCtx,
                                  engine: str = "explain_batch") -> str:
    """Resolve a segment-run-following job's run id to a concrete UUID.

    Shared by `explain_batch` and `matrix_batch` (both read one segment
    run instead of scope flags). Pass-through for concrete UUIDs.
    "latest" (or a missing/zero id) resolves to the newest segment run
    for (segment_mode, [segment_k], scope); no such run ->
    ManifestError with an actionable message.
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
            f"{engine}: no segment_runs row for"
            f" mode={mode!r} k={seg_k!r} in scope"
            " (run segments first)")
    return str(row["id"])


async def _resolve_explain_run_id(conn, params: dict,
                                   ctx: JobCtx) -> str:
    """Alias kept for explain_batch callers/tests."""
    return await _resolve_segment_run_id(conn, params, ctx,
                                         "explain_batch")


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


def _ml_repo_revision(ml_repo: Path) -> dict[str, Any]:
    """Best-effort git label for the ml checkout a row's numbers came from.

    Never raises: a tick must not fail because the checkout cannot be
    labelled. Both values are None when git is unavailable, the path is
    not a checkout, or the call itself fails -- so an unlabelled row
    means "could not resolve", never "old row".
    """
    import subprocess

    def _git(*args: str) -> str | None:
        try:
            proc = subprocess.run(["git", "-C", str(ml_repo), *args],
                                  capture_output=True, text=True,
                                  timeout=10)
        except Exception:  # noqa: BLE001 - a label, never a gate
            return None
        return proc.stdout if proc.returncode == 0 else None

    sha = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    return {"ml_repo_sha": (sha.strip() or None) if sha is not None else None,
            "ml_repo_dirty": (bool(status.strip())
                              if status is not None else None)}


def _upstream_payload(engine: str, ctx: JobCtx, cache: dict) -> str:
    """`pipeline_runs.upstream` for one job: engine + ml checkout revision.

    The revision is resolved once per run (first ml-engine job) and rides
    only on jobs that executed as a subprocess: a sha on a row whose
    numbers came from in-process data code would be a mislabel. A row a
    tick wrote also carries `tick` -- the same id on every row of one
    loop fire, absent on hand-runs, which is as close to "who wrote this"
    as the table gets (015 has no actor column).
    """
    import json

    payload: dict[str, Any] = {"engine": engine}
    if ctx.tick:
        payload["tick"] = ctx.tick
    if engine not in DATA_ENGINES and ctx.ml_repo is not None:
        if not cache:
            cache.update(_ml_repo_revision(ctx.ml_repo))
        payload.update(cache)
    return json.dumps(payload)


async def run_pipeline(conn, pipeline: str, jobs: dict[str, dict],
                       ctx: JobCtx, force: bool = False) -> dict[str, Any]:
    """Execute one pipeline's jobs in topo order, recording pipeline_runs."""
    order = topo_sort(jobs)
    outcomes: dict[str, dict[str, Any]] = {}
    ml_revision: dict[str, Any] = {}  # resolved once, first ml-engine job
    for name in order:
        job = jobs[name]
        engine = job["engine"]
        failed_upstream = [d for d in job["depends_on"]
                           if outcomes.get(d, {}).get("status")
                           not in ("succeeded", "skipped")
                           or outcomes.get(d, {}).get("blocking")]
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
            ctx.brand_id, ctx.group_id, ctx.sync_run_id)
        if run_row is None:
            # The gate refused the claim: another cursor owns this job for
            # this as_of right now. Skip it, and make sure its dependants
            # skip too -- running them beside an in-flight job reads
            # half-written tables and is the race this gate exists for.
            outcomes[name] = {
                "status": "skipped", "blocking": True,
                "error": "already running in another process"}
            continue
        rid = run_row["id"]
        upstream = _upstream_payload(engine, ctx, ml_revision)
        try:
            if engine in DATA_ENGINES:
                run_id = await _run_data_engine(engine, job["params"],
                                                conn, ctx)
                output_note = ""
            elif engine in ("explain_batch", "matrix_batch"):
                job_params = dict(job["params"])
                job_params["segment_run_id"] = \
                    await _resolve_segment_run_id(conn, job_params, ctx,
                                                  engine)
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
                               upstream, None)
            outcomes[name] = {"status": "succeeded", "run_id": run_id,
                              "note": output_note[-500:]
                              if engine not in DATA_ENGINES
                              else None}
        except Exception as exc:  # noqa: BLE001 - recorded on the run row
            await conn.execute(FINISH_RUN_SQL, rid, "failed", None,
                               upstream,
                               f"{type(exc).__name__}: {exc}"[:2000])
            outcomes[name] = {"status": "failed",
                              "error": f"{type(exc).__name__}: {exc}"[:500]}
    return {"pipeline": pipeline, "as_of": ctx.as_of.isoformat(),
            "outcomes": outcomes}
