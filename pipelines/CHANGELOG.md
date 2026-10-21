# pipelines — shipped work (a log, not a doc)

Dated entries of what landed. A log is not a doc: it carries no rule, and an entry that
becomes a practice moves into `pipelines/contracts.md` or the repo's `AGENTS.md` without
its date. The live gap list is in `pipelines/README.md` § Status.

## Status — SHIPPED 2026-10-22: the cohort writer re-measures its cells

- **`pipelines/snapshot.py`:** `COHORT_SNAPSHOT_UPSERT_SQL` now UPDATES the
  cells it measures (`ON CONFLICT ... DO UPDATE` on the grain index's
  expressions) instead of `DO NOTHING`, deletes the cells the population no
  longer produces (this granularity and scope only), and stamps every row it
  writes with the run's write time - so one grid carries ONE vintage.
  `POPULATION_RULE` names the population a row describes;
  `assert_cohorts_disjoint` + `COHORT_GRID_SIZE_SQL` fail the run when the
  grid's per-cohort sizes do not add up to the scope's distinct customers.
- **`db/migrations/056_cohort_population_rule.sql`:** additive
  `cohort_snapshots.population_rule` (its default is the writer's constant,
  byte for byte).
- **Checks:** `services/api/tests/test_cohort_writer.py` (six checks, each
  failing if the writer's change is reverted) and
  `scripts/mutation_check_cohort_writer.py` (the receipt).
- The contract and the reader's side: `pipelines/contracts.md` "020".

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
