"""Run one scheduled pipeline (cron entrypoint; data owns execution).

Manifests live in `pipeline_manifests` (seeded by migration 015, edited
via the app config API). --manifest-file is a DEV escape hatch: it runs
in-memory and NEVER writes the table.

  python scripts/run_pipeline.py --pipeline nightly --ml-repo ../crm-cdp-ml
  python scripts/run_pipeline.py --pipeline nightly --as-of 2026-10-08 --force
  python scripts/run_pipeline.py --watch --ml-repo ../crm-cdp-ml
  python scripts/run_pipeline.py --watch --once --now 2026-10-17T01:00

Exit 0 when every job succeeded or skipped; exit 1 otherwise.

`--watch` is the in-repo trigger: nothing else fires a tick today (no cron
entry on any host), so this loop re-reads `pipeline_manifests` every
`--interval` seconds and runs each row whose declared 5-field `schedule`
matches the **business** clock (Asia/Hong_Kong) once per window. Killing it
with SIGINT is a clean stop (exit 130): the runner is re-entrant, so a
window it was interrupted in is simply re-run by the next tick.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipelines import retention  # noqa: E402
from pipelines import schedule as sched  # noqa: E402
from pipelines.snapshot import (  # noqa: E402
    BUSINESS_TZ_OFFSET_HOURS, business_now, business_today)


def _uuid_or_none(v: str | None):
    return uuid.UUID(v) if v else None


def _pidfile_port(data_dir: Path) -> int | None:
    """Read the live postmaster port from a pgembed pidfile.

    pgembed picks a RANDOM port per restart, so a pinned port rots;
    the pidfile's 4th line is the current port. None when absent or
    unparsable — callers treat that as "not a live candidate".
    """
    try:
        lines = (data_dir / "postmaster.pid").read_text(
            encoding="utf-8").splitlines()
        return int(lines[3].strip())
    except (OSError, IndexError, ValueError):
        return None


def _db_url_candidates(cli_url: str | None) -> list[str]:
    """Tick-time DATABASE resolution, highest precedence first.

    --db-url > $DATABASE_URL > live pidfiles (app checkout first,
    then cwd). pgembed ports are random per restart, so pidfiles are
    re-read at every tick — never pinned. Deliberately NO boot path:
    spawning a fresh empty pgembed when nothing is live would fork
    the schedule onto a silent empty DB; fail loudly instead.
    """
    import os

    cands: list[str] = []
    for raw in (cli_url, os.environ.get("DATABASE_URL")):
        if raw:
            cands.append(raw)
    for data_dir in (REPO.parent / "crm-cdp-app" / ".pgembed-data",
                     Path.cwd() / ".pgembed-data"):
        port = _pidfile_port(data_dir)
        if port is not None:
            url = f"postgresql://postgres@127.0.0.1:{port}/postgres"
            if url not in cands:
                cands.append(url)
    return cands


async def _connect_resolved(cli_url: str | None):
    """Connect to the first live candidate; return (conn, url)."""
    import asyncpg

    cands = _db_url_candidates(cli_url)
    errors: list[str] = []
    for url in cands:
        try:
            conn = await asyncio.wait_for(asyncpg.connect(url), 5)
            return conn, url
        except (OSError, asyncio.TimeoutError,
                asyncpg.PostgresError) as exc:
            errors.append(f"{url}: {exc}")
    hint = "; ".join(errors) if errors else "no candidates"
    raise ValueError(
        "no live database for the tick (tried: "
        f"{hint}). Set --db-url or DATABASE_URL to the live postmaster, "
        "or start it (pgembed boot in the app checkout).")


async def run(pipeline: str, as_of: date, ml_repo: Path | None,
              brand_id, group_id, created_by: str | None,
              force: bool, manifest_file: Path | None,
              db_url: str | None, sync_run_id: str | None = None,
              tick: str | None = None) -> dict:
    conn, live_url = await _connect_resolved(db_url)
    try:
        if manifest_file is not None:
            print("warning: --manifest-file runs in-memory; "
                  "pipeline_manifests not updated", file=sys.stderr)
            yaml_text = manifest_file.read_text(encoding="utf-8")
        else:
            yaml_text = await conn.fetchval(sched.FETCH_MANIFEST_SQL,
                                            pipeline)
            if yaml_text is None:
                raise ValueError(
                    f"no manifest for pipeline {pipeline!r} "
                    f"(seed via 015 or the app config API)")
        jobs = sched.parse_manifest(yaml_text).get(pipeline)
        if not jobs:
            raise ValueError(f"pipeline {pipeline!r} defines no jobs")
        ctx = sched.JobCtx(pipeline=pipeline, as_of=as_of,
                           brand_id=brand_id, group_id=group_id,
                           created_by=created_by, ml_repo=ml_repo,
                           db_url=live_url, sync_run_id=sync_run_id,
                           tick=tick)
        return await sched.run_pipeline(conn, pipeline, jobs, ctx,
                                        force=force)
    finally:
        await conn.close()


def _tick_clock(raw: str | None) -> datetime:
    """The business clock for a due check (`--now` for dev/verification).

    Naive input is read as business time (Asia/Hong_Kong), never host
    time, so `--now 2026-10-17T01:00` means the nightly window on a box
    in any timezone.
    """
    if not raw:
        return business_now()
    when = datetime.fromisoformat(raw)
    if when.tzinfo is None:
        when = when.replace(
            tzinfo=timezone(timedelta(hours=BUSINESS_TZ_OFFSET_HOURS)))
    return when


def _log(event: dict) -> None:
    """One JSON line per decision (flushed: this runs for days)."""
    print(json.dumps(event, default=str), flush=True)


def _scope_label(brand, group) -> dict:
    return {"brand_id": str(brand) if brand is not None else None,
            "group_id": str(group) if group is not None else None}


async def _tick_scopes(db_url: str | None) -> list[tuple[Any, Any]]:
    """Every brand, then hq -- the scopes one unattended fire covers.

    A brand-scoped session can only see brand-stamped rows and an hq
    session only NULL/NULL ones (stamp equality, fail closed), so one fire
    per scope is what makes the same pipeline visible to both.
    """
    conn, _live = await _connect_resolved(db_url)
    try:
        return await sched.tick_scopes(conn)
    finally:
        await conn.close()


async def watch(interval: int, ml_repo, brand_id, group_id,
                created_by: str | None, force: bool, db_url: str | None,
                once: bool, now: datetime | None,
                only: list[str] | None = None) -> int:
    """Wait for the next due window, run what is due, forever.

    Every decision is one JSON line on stdout -- which manifests were due,
    which fired, and each fire's per-job statuses -- because a tick that
    runs for days has no other record. A database that is not live is a
    logged failure and another wait, never a fresh empty boot.

    Idempotence is the RUNNER's, not the loop's: `pipeline_runs` is unique
    per (pipeline, job, as_of, scope) and a succeeded row skips, so a
    second tick -- another process, a restart, a duplicate cron entry --
    for a window already succeeded writes nothing new. `fired` only stops
    a loop checking more often than once a minute from re-entering the
    same window in one process; a *failed* window is retried once per
    process start instead of every `--interval` seconds, since a failure
    is a row carrying `error`, not something to hammer.

    Rows carry `upstream.tick` (one id per fire): 015 `pipeline_runs` has
    no actor column, so this is the attribution the table can hold --
    tick-written rows are distinguishable from hand-runs, rows within one
    fire are not. SIGINT leaves an in-flight job's row `running` with no
    `finished_at`; the next tick retries it (skip needs `succeeded`), so an
    interrupted window self-heals and that row is the evidence.
    """
    fired: set[tuple[str, date]] = set()
    failures = 0
    while True:
        when = now if now is not None else business_now()
        as_of = when.date()
        try:
            conn, _live = await _connect_resolved(db_url)
        except (ValueError, OSError) as exc:
            _log({"tick": when.isoformat(), "event": "no database",
                  "error": str(exc)})
            if once:
                return 2
            await asyncio.sleep(interval)
            continue
        try:
            rows = [(r["pipeline"], r["yaml"])
                    for r in await conn.fetch(sched.FETCH_MANIFESTS_SQL)]
        finally:
            await conn.close()
        due_now: list[str] = []
        for pipeline, yaml_text in rows:
            if only and pipeline not in only:
                continue
            try:
                names = sched.due_pipelines(yaml_text, when)
            except sched.ManifestError as exc:
                _log({"tick": when.isoformat(), "pipeline": pipeline,
                      "event": "bad manifest", "error": str(exc)})
                continue                       # one bad row, keep ticking
            if not names:
                continue
            if pipeline not in names:
                _log({"tick": when.isoformat(), "pipeline": pipeline,
                      "event": "row name differs from its yaml",
                      "declared": names})
                continue
            key = (pipeline, as_of)
            if key in fired:
                _log({"tick": when.isoformat(), "pipeline": pipeline,
                      "as_of": as_of.isoformat(),
                      "event": "already fired for this window"})
                continue
            fired.add(key)
            due_now.append(pipeline)
            tick_id = uuid.uuid4().hex[:12]
            # Scope is a run-level dimension, so "visible to a brand
            # session" is one fire per brand plus the hq batch -- unless
            # the caller pinned a scope, which is a deliberate hand-run of
            # exactly that scope.
            pinned = brand_id is not None or group_id is not None
            scopes = ([(brand_id, group_id)] if pinned
                      else await _tick_scopes(db_url))
            for scope_brand, scope_group in scopes:
                try:
                    out = await run(pipeline, as_of, ml_repo, scope_brand,
                                    scope_group, created_by, force, None,
                                    db_url, None, tick_id)
                except Exception as exc:          # unattended: keep going
                    failures += 1
                    _log({"tick": when.isoformat(), "pipeline": pipeline,
                          "as_of": as_of.isoformat(), "tick_id": tick_id,
                          "scope": _scope_label(scope_brand, scope_group),
                          "event": "fire failed",
                          "error": f"{type(exc).__name__}: {exc}"})
                    continue
                bad = {n: o["status"] for n, o in out["outcomes"].items()
                       if o["status"] not in ("succeeded", "skipped")}
                failures += len(bad)
                _log({"tick": when.isoformat(), "pipeline": pipeline,
                      "as_of": as_of.isoformat(), "tick_id": tick_id,
                      "scope": _scope_label(scope_brand, scope_group),
                      "event": "fired", "jobs": len(out["outcomes"]),
                      "failed": bad or None})
        # Housekeeping last, never before the scores, and never fatal: a
        # retention bug must not cost a night of analytics. The line carries the
        # full per-parent map unchanged -- `{}` reads as "ran, nothing aged",
        # which is a different fact from the line being absent (did not run) --
        # and `failed` lists the parents that raised (`-1` in the map), so a
        # night where every parent failed cannot look like a quiet one.
        if due_now:
            try:
                rconn, _ = await _connect_resolved(db_url)
                try:
                    deleted = await retention.prune(rconn)
                finally:
                    await rconn.close()
                _log({"tick": when.isoformat(), "event": "retention",
                      "deleted": deleted,
                      "failed": [t for t, n in deleted.items() if n < 0]})
            except Exception as exc:       # noqa: BLE001 -- see above
                _log({"tick": when.isoformat(),
                      "event": "retention failed",
                      "error": f"{type(exc).__name__}: {exc}"})
        if once:
            # A `running` row past RUN_STALE_AFTER is a job that died; naming
            # them here is the operator's side of the same predicate a reader
            # uses, so nobody has to read a log to find a hung night.
            stale: list[dict] = []
            try:
                sconn, _ = await _connect_resolved(db_url)
                try:
                    stale = await sched.stale_running(sconn)
                finally:
                    await sconn.close()
            except Exception as exc:       # noqa: BLE001 -- see above
                _log({"tick": when.isoformat(),
                      "event": "stale check failed",
                      "error": f"{type(exc).__name__}: {exc}"})
            _log({"tick": when.isoformat(), "event": "once: done",
                  "fired": sorted(f"{p}@{d}" for p, d in fired),
                  "failures": failures,
                  "stale": [{"pipeline": r["pipeline"], "job": r["job"],
                             "as_of": r["as_of"].isoformat(),
                             "running_for_s": int(
                                 r["running_for"].total_seconds())}
                            for r in stale] or None})
            return 1 if failures else 0
        # one line per pass, even when nothing is due: a loop that logs only
        # its fires is indistinguishable from a loop that died
        _log({"tick": when.isoformat(), "event": "pass",
              "rows": len(rows), "due": due_now or None})
        await asyncio.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="Run one scheduled pipeline")
    ap.add_argument("--pipeline", default=None,
                    help="one-shot: the manifest row to run (in --watch the"
                         " stored schedules decide instead)")
    ap.add_argument("--as-of", default=None,
                    help="YYYY-MM-DD (default: business today, Asia/Hong_Kong)")
    ap.add_argument("--ml-repo", type=Path, default=None,
                    help="path to the crm-cdp-ml checkout (ml engines)")
    ap.add_argument("--brand-id", default=None)
    ap.add_argument("--group-id", default=None)
    ap.add_argument("--created-by", default="scheduler")
    ap.add_argument("--force", action="store_true",
                    help="re-run succeeded jobs")
    ap.add_argument("--manifest-file", type=Path, default=None,
                    help="DEV ONLY: run this YAML without touching the table")
    ap.add_argument("--db-url", default=None,
                    help="DATABASE_URL override (default: $DATABASE_URL,"
                    " else live pgembed pidfile at tick time)")
    ap.add_argument("--sync-run-id", default=None,
                    help="LD3: feeding connector_sync_runs id (lineage)")
    ap.add_argument("--watch", action="store_true",
                    help="loop: fire each manifest whose schedule matches the"
                         " business clock (the in-repo tick)")
    ap.add_argument("--interval", type=int, default=60,
                    help="seconds between due checks in --watch (default 60)")
    ap.add_argument("--once", action="store_true",
                    help="one due check against the stored schedules, then"
                         " exit (the unattended tick entry point)")
    ap.add_argument("--only", default=None,
                    help="with --watch/--once: comma-separated manifest rows"
                         " to consider (default: all)")
    ap.add_argument("--now", default=None,
                    help="ISO datetime pinning the tick clock (dev/"
                         "verification; naive = Asia/Hong_Kong)")
    args = ap.parse_args()
    if args.watch and args.pipeline:
        ap.error("--pipeline is one-shot; --watch uses the manifests'"
                 " schedules (filter with --only)")
    if args.once and args.pipeline:
        ap.error("--pipeline is one-shot; --once runs every due pipeline"
                 " from the manifests (filter with --only)")
    if not args.watch and not args.once and not args.pipeline:
        ap.error("--pipeline is required (or --watch / --once for the loop)")
    if args.watch and args.interval < 1:
        ap.error("--interval must be >= 1 second")
    if args.watch or args.once:          # --once is one pass of this loop
        only = ([p.strip() for p in args.only.split(",") if p.strip()]
                if args.only else None)
        pinned = _tick_clock(args.now) if args.now else None
        if pinned is not None and not args.once:
            print("warning: --now pins the tick clock; every pass sees the"
                  " same window", file=sys.stderr)
        try:
            rc = asyncio.run(watch(args.interval, args.ml_repo,
                                   _uuid_or_none(args.brand_id),
                                   _uuid_or_none(args.group_id),
                                   args.created_by, args.force, args.db_url,
                                   args.once, pinned, only))
        except KeyboardInterrupt:
            print("interrupted: the in-flight job's row is marked 'failed'",
                  "with the signal, so the next tick finds it without a log",
                  flush=True)
            raise SystemExit(130)
        raise SystemExit(rc)
    as_of = (date.fromisoformat(args.as_of) if args.as_of
             else business_today())
    try:
        out = asyncio.run(run(args.pipeline, as_of, args.ml_repo,
                              _uuid_or_none(args.brand_id),
                              _uuid_or_none(args.group_id),
                              args.created_by, args.force,
                              args.manifest_file, args.db_url,
                              args.sync_run_id))
    except (ValueError, sched.ManifestError) as exc:
        print(f"failed: {exc}")
        raise SystemExit(2)
    print(json.dumps(out, indent=2, default=str))
    failed = [n for n, o in out["outcomes"].items()
              if o["status"] not in ("succeeded", "skipped")]
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
