"""Run one scheduled pipeline (cron entrypoint; data owns execution).

Manifests live in `pipeline_manifests` (seeded by migration 015, edited
via the app config API). --manifest-file is a DEV escape hatch: it runs
in-memory and NEVER writes the table.

  python scripts/run_pipeline.py --pipeline nightly --ml-repo ../crm-cdp-ml
  python scripts/run_pipeline.py --pipeline nightly --as-of 2026-10-08 --force

Exit 0 when every job succeeded or skipped; exit 1 otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipelines import schedule as sched  # noqa: E402


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
              db_url: str | None, sync_run_id: str | None = None) -> dict:
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
                           db_url=live_url, sync_run_id=sync_run_id)
        return await sched.run_pipeline(conn, pipeline, jobs, ctx,
                                        force=force)
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Run one scheduled pipeline")
    ap.add_argument("--pipeline", required=True)
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD (default: today)")
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
    args = ap.parse_args()
    as_of = (date.fromisoformat(args.as_of) if args.as_of
             else date.today())
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
