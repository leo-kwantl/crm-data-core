# Data integration pipeline (CRM-CDP ingest)

Owns everything that lands rows in CRM core tables. The API (`crm-cdp-app`)
and ML (`crm-cdp-ml`) consume; they never write ingestion logic.

## Contracts (mirror of `crm-cdp-app`, app is source of truth for DDL)

- `customers.member_id` must match `^M-[0-9]{6}$` (CHECK). Reject, don't coerce.
- `orders.store_id` NOT NULL — resolve/create the store before the order.
- Identity: `external_member_id` + `member_id` substring fallback (escaped
  ILIKE, LIMIT 20); phone/email are exact-only (PII policy, `docs/conventions.md`).
- Events: append-only (`customer_events`, `consents`); never UPDATE/DELETE.
- All migrations additive-only (`db/migrations`, dbmate plain SQL).

## Layout

- `pipelines/ingest/orders.py` — order ingest: validate store, normalize lines, upsert idempotently.
- `pipelines/ingest/events.py` — customer/consent event append path.
- `pipelines/ingest/identity.py` — external-ID binding + member-id resolution.
- `pipelines/contracts.md` — field-level source→table mapping + idempotency keys.
- `db/` — full migration mirror (app owns `001-009`-era base; data owns future `01x_ingest_*`).
- `services/` — API harness mirror so ingestion tests run standalone (app owns).

## Run

```bash
pip install -r services/api/requirements.txt
pytest services/api/tests/test_orders.py services/api/tests/test_customer_events.py services/api/tests/test_identity.py
```

## Roadmap

CDC/outbox from source systems → staging tables → validated upsert. See
`openspec/changes/data-pipeline-init/proposal.md`.

## Status — missing pieces (documented, NOT built; follow-ups)

- **Quarantine:** no quarantine tables and no quarantine write path —
  `normalize()` raises `ValueError` today; the contracts.md "rejects go to
  quarantine" rule has no backing store or code yet.
- **Staging tables:** none exist; loads validate inline, no land-then-validate step.
- **CDC/outbox:** no source-system CDC or outbox consumer exists.
- **Backfill/reconciliation:** no backfill runner, no source-vs-table
  reconciliation job.
- **Reject-path unit tests:** `normalize()` happy paths are exercised via
  the API harness tests, but there are no dedicated unit tests for the
  `ValueError` reject paths (bad member_id, missing store, empty lines,
  missing dedup keys).

## Status — SHIPPED 2026-10-09: daily snapshot history

- **`013_analytics_history.sql`:** `clv/churn_snapshots` (+ per-customer
  scores), `kpi_daily_snapshots` (1 row/day/scope), `model_training_runs`.
  Immutable + scope-tagged like 010/011/012; idempotent per day x scope
  via COALESCE UNIQUE indexes (plain UNIQUE allows dup NULLs).
- **`pipelines/snapshot.py`:** `pct_change` / `kpi_movement` / `clv_movement`
  + `KPI_DAILY_UPSERT_SQL` / `KPI_TREND_SQL`.
- **`scripts/run_daily_snapshot.py`:** `--as-of YYYY-MM-DD` daily job
  (INSERT-only, safe on live; re-run = no-op).
- **`db/queries/kpi_daily.sql` + `services/api/tests/test_snapshots.py`**.
- ML `run_snapshot` / `run_recs_batch` stay the compute side; their
  outputs now have history tables here + `model_training_runs` lineage.
- Scheduler engines (`pipelines/schedule.py`): `rfm_snapshot` = legacy v1
  writer (append-only, one window); **`ld8_snapshot` = v2 four-window
  writer and the canonical daily RFM capture** (see contracts.md
  "Daily RFM writer").

## Status — SHIPPED 2026-10-09: batch upload ETL

- **`014_upload_staging.sql`:** `upload_jobs` (hash-deduped per scope) +
  `staging_rows` (land-then-validate) + `quarantine_rows` (reject store);
  plus the missing idempotency columns (`orders.source/external_order_id`,
  `customer_events.source_event_id`, partial UNIQUEs).
- **`pipelines/upload.py`:** `file_hash` / `parse_csv` / `normalize_customer` /
  `validate_batch` / `reconcile` + job SQL. Kinds: customers, orders,
  events, identity.
- **`scripts/run_upload.py`:** `run_upload.py <kind> <file>` daily/file job
  (validate-only by default → status `validated`, core untouched;
  `--apply` writes core idempotently). INSERT-only without `--apply`,
  live-safe; re-upload = same job.
- **`services/api/tests/test_upload.py`** (7 unit + 1 pgembed idempotency).
- Next: core-table apply of staged `ok` rows (store/customer resolution).

## Status — SHIPPED 2026-10-17: unattended tick (`--watch` / `--once`)

- **`scripts/run_pipeline.py --once [--only a,b]`** — one pass: reads
  `pipeline_manifests`, fires every row whose stored `schedule` matches
  the business clock, logs one JSON line per decision, exits 0 (2 = no
  live DB, 1 = a job failed). **This is the unattended entry point**: point
  any at-least-once-a-minute trigger at it, e.g. a Windows Task Scheduler
  entry — `schtasks /create /tn crm-tick /sc minute /mo 1 /tr
  "python <repo>\scripts\run_pipeline.py --once --ml-repo
  <repo>\..\crm-cdp-ml"` — or a cron line. A second trigger, a restart or
  a competing host is safe: a `succeeded` window skips in the runner.
- **`--watch [--interval 60]`** — the same pass in a loop until Ctrl-C
  (exit 130; an in-flight job's row stays `running` and the next tick
  retries it). Use it when supervision is external (pm2 / nssm /
  systemd); use `--once` when the host owns the cadence.
- Rows written by a tick carry `upstream.tick` (one id per fire); rows
  from `--pipeline` hand-runs do not. contracts.md §015 states the
  evaluator's cron semantics and why the table is a declaration, not the
  authority.
- Tests: `services/api/tests/test_schedule.py` — cron match/reject, due
  selection, live manifest rows reachable by the tick, `upstream.tick`
  stamping, the CLI's `--once` line + exit code, and Ctrl-C exit 130.
