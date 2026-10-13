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
from services.api.db import get_database_url  # noqa: E402


def _uuid_or_none(v: str | None):
    return uuid.UUID(v) if v else None


async def run(pipeline: str, as_of: date, ml_repo: Path | None,
              brand_id, group_id, created_by: str | None,
              force: bool, manifest_file: Path | None) -> dict:
    import asyncpg

    conn = await asyncpg.connect(get_database_url())
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
                           db_url=get_database_url())
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
    args = ap.parse_args()
    as_of = (date.fromisoformat(args.as_of) if args.as_of
             else date.today())
    try:
        out = asyncio.run(run(args.pipeline, as_of, args.ml_repo,
                              _uuid_or_none(args.brand_id),
                              _uuid_or_none(args.group_id),
                              args.created_by, args.force,
                              args.manifest_file))
    except (ValueError, sched.ManifestError) as exc:
        print(f"failed: {exc}")
        raise SystemExit(2)
    print(json.dumps(out, indent=2, default=str))
    failed = [n for n, o in out["outcomes"].items()
              if o["status"] not in ("succeeded", "skipped")]
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
