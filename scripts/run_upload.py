"""Batch upload job (data owns ETL; app owns the HTTP file edge).

  python scripts/run_upload.py orders ./pos_orders.csv --source pos
  python scripts/run_upload.py customers ./members.csv --created-by HQ

Stages: receive (hash + job row, same file re-upload = same job, no
 double-commit) -> stage (raw lines) -> validate (ingest normalize) ->
 [optional --apply: core-table apply per pipelines/apply.py] ->
 finish (quarantine rejects persist, counts + status update).

 Exit 0 on committed / committed_with_rejects / validated;
 exit 2 on failed/empty.
 Safe on live WITHOUT --apply (INSERT-only into upload/staging/
 quarantine, never touches core tables). WITH --apply it writes core
 tables idempotently (re-apply = no-op skips) inside one transaction.
 """
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipelines import apply as ap  # noqa: E402
from pipelines import upload as up  # noqa: E402
from services.api.db import get_database_url  # noqa: E402


def _uuid_or_none(v: str | None):
    return uuid.UUID(v) if v else None


async def run(path: Path, kind: str, source: str, created_by: str | None,
              brand_id, group_id, apply: bool = False) -> dict:
    import asyncpg

    data = path.read_bytes()
    digest = up.file_hash(data)
    text, encoding = up.decode_upload_bytes(data)
    rows = up.parse_csv(text, kind)
    conn = await asyncpg.connect(get_database_url())
    job_id = None
    try:
        found = await conn.fetchrow(up.FIND_JOB_SQL, digest, kind,
                                    brand_id, group_id)
        if found is not None and found["status"] in (
                "committed", "committed_with_rejects"):
            return {"job_id": str(found["id"]), "reused": True,
                    "status": found["status"]}
        job_id = None
        if found is not None:
            job_id = found["id"]
        else:
            job_id = await conn.fetchval(
                up.CREATE_JOB_SQL, path.name, digest, kind, "v1", source,
                len(rows), created_by, brand_id, group_id)
            if job_id is None:  # lost the insert race: re-read winner
                found = await conn.fetchrow(up.FIND_JOB_SQL, digest, kind,
                                            brand_id, group_id)
                assert found is not None
                return {"job_id": str(found["id"]), "reused": True,
                        "status": found["status"]}
        for r in rows:
            await conn.execute(up.STAGE_ROWS_SQL, job_id, r["row_number"],
                               json.dumps(r["raw"]))
        ok, bad = up.validate_batch(rows, kind)
        for o in ok:
            await conn.execute(up.MARK_ROWS_SQL, job_id, o["row_number"],
                               "ok", None)
        for b in bad:
            await conn.execute(up.MARK_ROWS_SQL, job_id, b["row_number"],
                               "rejected", b["reason"])
            await conn.execute(up.WRITE_QUARANTINE_SQL, job_id,
                               b["row_number"], source, b["key"],
                               b["reason"], json.dumps(b["raw"]),
                               b["code"])
        up.reconcile(len(rows), len(ok), len(bad))
        applied = skipped = revived = 0
        apply_failed: list = []
        async with conn.transaction():
            if apply:
                res = await ap.apply_job(conn, job_id, kind, source)
                applied, skipped = res["applied"], res["skipped"]
                apply_failed, revived = res["failed"], res["customers_revived"]
        committed = applied + skipped
        quarantined = len(bad) + len(apply_failed)
        if not apply:
            # validate-only: rows rest in staging; committed = staged-ok,
            # status stays 'validated' until a later --apply run.
            committed, quarantined = len(ok), len(bad)
            status = "validated"
        else:
            status = ("committed" if not quarantined
                      else "committed_with_rejects")
        reasons = await up.reason_counts(conn, job_id)
        await conn.execute(up.FINISH_JOB_SQL, job_id, status, len(rows),
                           len(rows), committed, quarantined, None,
                           json.dumps(reasons))
        return {"job_id": str(job_id), "reused": False,
                "status": status, "encoding": encoding,
                "total": len(rows), "committed": committed,
                "quarantined": quarantined, "applied": applied,
                "skipped": skipped, "customers_revived": revived,
                "quarantine_reasons": reasons,
                "apply_failed": [f["row_number"] for f in apply_failed]}
    except Exception as exc:  # noqa: BLE001 - job row must record failure
        if job_id is not None:
            try:
                await conn.execute(up.FINISH_JOB_SQL, job_id, "failed", 0,
                                   0, 0, 0, str(exc)[:1000], "{}")
            except Exception:
                pass
        raise
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage + validate one upload file")
    ap.add_argument("kind", choices=list(up.KINDS))
    ap.add_argument("file", type=Path)
    ap.add_argument("--source", default="upload")
    ap.add_argument("--created-by", default=None)
    ap.add_argument("--brand-id", default=None)
    ap.add_argument("--group-id", default=None)
    ap.add_argument("--apply", action="store_true",
                    help="apply staged ok rows to core tables "
                         "(idempotent; default validates only)")
    args = ap.parse_args()
    try:
        out = asyncio.run(run(args.file, args.kind, args.source,
                              args.created_by,
                              _uuid_or_none(args.brand_id),
                              _uuid_or_none(args.group_id),
                              apply=args.apply))
    except ValueError as exc:
        print(f"failed: {exc}")
        raise SystemExit(2)
    print(json.dumps(out, indent=2))
    raise SystemExit(0)


if __name__ == "__main__":
    main()
