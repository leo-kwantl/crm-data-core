# crm-cdp-data — DDL + ingest owner

Owns the core data + **ALL DDL** (`db/migrations/`: `001`→`012` including analytics *result* tables, plus future `01x_ingest_*`) and ingestion/ETL (`pipelines/ingest/{orders,events,identity}.py` + `pipelines/contracts.md`).

- **Owns:** all migrations (additive-only, dbmate plain SQL), seed `demo.sql`, query mirrors in `db/queries/`, ingestion contracts + normalize/load code.
- **Does NOT own:** analytics/ML features (compute SQL lives in `crm-cdp-ml`'s ml_engine; data keeps DDL for the result tables only), serving (reads happen in `crm-cdp-app`, which consumes this DDL).
- **No servers, no UI in this repo.** `services/` is a test-harness mirror (boots the API via httpx ASGI so the 4 ingest tests run standalone) — never serve from it. Never run `scripts/bootstrap_db.py` against a live DB (it TRUNCATEs + reseeds demo data; live migrations must be migrate-only).

## Layout

```
pipelines/ingest/{orders,events,identity}.py  # ingest: validate → normalize → load
pipelines/contracts.md                        # field-level source→table mapping + idempotency keys
pipelines/README.md                           # ingest scope, contracts summary, missing-pieces status
db/migrations/001–012 (+ future 01x_ingest_*) # ALL DDL — this repo is the owner
db/queries/                                   # raw-SQL mirrors (compute SQL lives in crm-cdp-ml)
db/seeds/demo.sql                             # demo dataset (never mixed with real rows)
services/api/                                 # harness mirror: 4 ingest tests boot via ASGI
scripts/bootstrap_db.py                       # DEV/CI ONLY — migrate + reseed demo (see banner)
docs/conventions.md                           # data deltas only; cross-repo conventions at ../docs/conventions.md
openspec/changes/data-pipeline-init/          # pipeline proposal + tasks
```

See `pipelines/README.md` for ingest detail.

## Verify (no live servers touched)

```bash
export DATABASE_URL=   # ensure unset → tests boot ephemeral pgembed
pytest services/api/tests -q
python -c "import pipelines.ingest.orders, pipelines.ingest.events, pipelines.ingest.identity"
```

## Siblings (read-only — never edit)

- `../crm-cdp-app` serves (reads this DDL).
- `../crm-cdp-ml` computes (reads/writes analytics result tables).
- `../crm-cdp` is the frozen master source for any copies (never modify).
