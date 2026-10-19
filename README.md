# crm-cdp-data — DDL + ingest owner

Owns the core data + **ALL DDL** (`db/migrations/`: every migration file, including analytics *result* tables and future `01x_ingest_*`) and ingestion/ETL (`pipelines/ingest/{orders,events,identity}.py` + `pipelines/contracts.md`).

- **Owns:** all migrations (additive-only plain SQL; the runner applies each file's up-section in filename order and keys the version by the file stem — `../docs/conventions.md` § Naming), seed `demo.sql`, query mirrors in `db/queries/`, ingestion contracts + normalize/load code.
- **Does NOT own:** analytics/ML features (compute SQL lives in `crm-cdp-ml`'s ml_engine; data keeps DDL for the result tables only), serving (reads happen in `crm-cdp-app`, which consumes this DDL).
- **No servers, no UI in this repo.** `services/` is a test-harness mirror (boots the API via httpx ASGI so the ingest tests run standalone) — never serve from it. Never run `scripts/bootstrap_db.py` against a live DB (it TRUNCATEs + reseeds demo data; live migrations must be migrate-only).

## Layout

```
pipelines/ingest/{orders,events,identity}.py  # ingest: validate → normalize → load
pipelines/contracts.md                        # field-level source→table mapping + idempotency keys (TOC at the top)
pipelines/citation-rules.md                   # how to cite here; enforced by scripts/audit_doc_cites.py
pipelines/README.md                           # ingest scope, contracts summary, missing-pieces status
pipelines/CHANGELOG.md                        # what landed, dated (a log, not a doc)
db/migrations/                                 # ALL DDL — this repo is the owner
db/queries/                                   # raw-SQL mirrors (compute SQL lives in crm-cdp-ml)
db/seeds/demo.sql                             # demo dataset (never mixed with real rows)
services/api/                                 # harness mirror: the ingest tests boot via ASGI
scripts/bootstrap_db.py                       # DEV/CI ONLY — migrate + reseed demo (see banner)
scripts/audit_doc_cites.py                    # checks every cite in the docs against the trees it names
docs/conventions.md                           # data deltas only; cross-repo conventions at ../docs/conventions.md
openspec/changes/data-pipeline-init/          # pipeline proposal + tasks
```

See `pipelines/README.md` for ingest detail.

## Verify (no live servers touched)

`AGENTS.md` § Run / verify is the canonical command; `../tools/verify_all.py` runs it for
all three repos. `DATABASE_URL` stays unset, so the tests boot their own throwaway pgembed
and never touch the live cluster.

## Siblings (read-only — never edit)

- `../crm-cdp-app` serves (reads this DDL).
- `../crm-cdp-ml` computes (reads/writes analytics result tables).
- Mirrored files (`db/**`, `services/api/**`) take `../crm-cdp-app`'s checkout as the base — copy byte-for-byte, never fork (`AGENTS.md` § Repo rules).
