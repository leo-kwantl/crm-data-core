# Source → table contracts

## customers

| source field | table column | rule |
|---|---|---|
| member code | `member_id` | must match `^M-[0-9]{6}$`, else reject row + quarantine |
| source key | `external_member_id` | unique per source; second-see is update, not insert |
| phone/email | `phone`, `email` | exact-only matching (PII: no partial/substring search) |

## orders

| source field | table column | rule |
|---|---|---|
| store code | `store_id` | NOT NULL; resolve via store map, create store if whitelisted |
| source order key | `external_order_id` | idempotency key; replay = no-op |
| line amounts | `order_lines` | integer cents; `total_cents = Σ qty × unit_cents` |

## events / consents

Append-only. Dedup key `(source, source_event_id)`. Consumers (app timeline,
ML features) read; nobody updates.

### kind vocabulary (additive; new kinds need ml ack before writing)

- Manual kinds (source `manual`, written by app): `note`, `visit`, `call`,
  `complaint`, `follow_up`.
- Derived kinds (source `derived`, written by data): `order_placed` — one
  row per order, `source_event_id = 'order:' || order_id`, `occurred_at =
  orders.ordered_at`, payload `{order_id, total_cents, store_id}`.
- ML calibration EXCLUDES `order_placed` by name (`store_churn` activity
  calibration filters `ce.kind <> 'order_placed'`) so derived rows can
  never double-count orders under activity='all'. Renaming the derived
  kind, or adding any further derived kind, requires ml ack FIRST — the
  exclusion is name-keyed.

## Failure handling

Rejects go to quarantine with (source, key, reason, raw) — never silently
dropped, never auto-retried without a rule change.

## Snapshot history (serves downstream trends + training)

RFM snapshots (`rfm_snapshots`) + rec batches (`rec_batches`) are the
existing persisted runs. `013_analytics_history.sql` adds the missing
history so movement / pct-change / training lineage work across days:

| run table | grain | idempotency |
|---|---|---|
| `clv_snapshots` + `clv_customer_scores` | one run per `as_of` x horizon x scope | UNIQUE `(as_of, horizon_days, scope)` |
| `churn_snapshots` + `churn_customer_scores` | one run per `as_of` x activity x horizon x scope | UNIQUE `(as_of, activity, horizon_days, scope)` |
| `kpi_daily_snapshots` | ONE ROW per day x scope (totals + day deltas + active_30d) | UNIQUE `(snapshot_date, scope)`; re-run = no-op |
| `model_training_runs` | which snapshot/params trained which model | append-only log |

Math lives in `pipelines/snapshot.py` (`pct_change`, `kpi_movement`,
`clv_movement` + `KPI_DAILY_UPSERT_SQL` / `KPI_TREND_SQL`).
Daily job: `python scripts/run_daily_snapshot.py --as-of YYYY-MM-DD`
(+ ML `run_snapshot` / `run_recs_batch`, then log to
`model_training_runs`). Queries in `db/queries/kpi_daily.sql`.
Scope columns (`brand_id`, `group_id`, NULL = group-wide) match 012.

## Batch upload ETL (frontend files land here)

App posts the file edge (`upload_jobs` row); data owns stage → validate
→ quarantine → commit. `014_upload_staging.sql` + `pipelines/upload.py`
+ `scripts/run_upload.py`. Flow per file (idempotent on
`file_hash` x kind x scope — re-upload = same job, no double-commit):

1. **receive:** SHA-256 hash + `upload_jobs` row (`received`).
2. **stage:** raw CSV lines → `staging_rows` (`pending`). CSV only in v1;
   gzip accepted (magic-sniffed, 10MB raw / 64MB decompressed caps).
   orders `lines` column carries a JSON list (cents may be decimal units).
3. **validate:** per-kind `pipelines/ingest` normalize (`customers` /
   `orders` / `events` / `identity`) → `ok` / `rejected`.
4. **apply (`--apply`, `pipelines/apply.py`):** staged `ok` rows into
   core tables, idempotently, one transaction per job. Customers
   upsert on `member_id` (+ contacts append, conflict-ignore); orders
   resolve store (id-or-code) + customer (uuid/member/external) +
   SKUs, pre-checking `(source, external_order_id)` so replay = skip;
   events pre-check `(source, source_event_id)`; identity binds the
   external id onto the existing member row. Unresolvable refs
   (unknown store/customer/SKU/member) → apply-fail quarantine, so
   `total == committed + quarantined` still holds
   (committed = applied + dedup-skipped). Optional raw `ordered_at`
   (ISO) honoured for orders, else now().
5. **finish:** rejects → `quarantine_rows` (source, key, reason, raw) +
   job counts; invariant `total == committed + quarantined`
   (`reconcile()` raises on drift). Without `--apply` the job ends
   `validated` (staged-ok counted committed, core untouched); with it,
   `committed` / `committed_with_rejects` / `failed`.

014 also backfills the idempotency columns the contracts always assumed:
`orders(source, external_order_id)` + partial UNIQUE, and
`customer_events.source_event_id` + partial UNIQUE `(source,
source_event_id)`. Core-table apply is SHIPPED (`pipelines/apply.py` +
`--apply`); the app-side thin `POST /uploads` edge is the remaining
piece.

## 015: segments + scheduled pipelines (ml part 2, data-owned tables)

`015_segments_pipelines.sql`: `segment_runs` (as_of/mode purchase|
overall/k/seed/centroids + features jsonb/inertia/rules_version/scope,
COALESCE-scope UNIQUE) + `segment_assignments` ((run, customer) PK,
segment/label; customer_id only per 013) + `pipeline_runs`
(pipeline/job/as_of/queued|running|succeeded|failed|skipped/run_id/
upstream/error/scope, UNIQUE) + `pipeline_manifests` (one validated YAML
row per pipeline; edits via app config API, never migrations).
Centroids are TRANSFORMED-space (purchase L2-taste / overall zscore);
the space tag rides in `model_training_runs.params` (ml writes the
lineage row, model='segment'). Inertia is run-quality only, never a
gate. Seeds: nightly + weekly content-verbatim from the agreed example
YAML (split per pipeline, ON CONFLICT DO NOTHING so admin edits win).

Runner: `pipelines/schedule.py` + `scripts/run_pipeline.py --pipeline
<name> --ml-repo <path>` (cron entrypoint). Tick-time DB resolution:
`--db-url` > `$DATABASE_URL` > live pgembed pidfiles re-read every
tick (app checkout, then cwd) — ports are random per restart, never
pinned; no live postmaster = loud failure, never a fresh empty boot.
Manifest parsed from the
table (`--manifest-file` dev override never writes). Topo order over
depends_on (cycle = error); data engines in-process (upload_apply =
sweep validated jobs, kpi_daily = KPI upsert, cohort_snapshot =
retention upserts per granularity — `DATA_ENGINES`; params
`granularities` + `max_offsets`, defaults week/month/year 12/12/5);
ml engines as
`python -m ml_jobs.run_* DB_URL ...` subprocesses (thread-based: safe
on SelectorEventLoop) with DATABASE_URL passthrough; stdout run-id
parsed, else NULL. Re-runnable: succeeded skipped unless `--force`;
upstream failure → downstream `skipped`. Cron owns the tick; scopes
fan-out is future (runner takes one --brand/--group per invocation).

## 016: modeling-upgrade flag mapping (ml registry / algorithms / signals)

`016_segment_behavior_mode.sql` widens `segment_runs.mode` to
purchase|overall|behavior (additive). Runner maps the new ml argv:
churn/clv `--model` (default bgnbd, pass-through — ml validates against
the registry); segments `--mode` + `--algorithm`
(kmeans|gmm|agglomerative|dbscan|hdbscan, default kmeans); recs
`--content` (default off) + `--include-user-cf` when true. Seeds
untouched (example YAML still validates). App mirrors note: app carries
verbatim 013/015 CREATEs for isolated test bootstrap — re-mirror 016
on update; live dev DB needs 016 applied (through 015 already).

## 019: source connector framework (D1, [BREAKING ingest])

`019_connectors.sql` + `pipelines/ingest/connectors.py`. Source types
are equals — `shopify | csv | excel | delta_lake | rest_api`
(`woocommerce` RESERVED: registration rejected until its spec lands;
widening the CHECK is the additive path). Every source funnels into the
same normalize/load contracts above; per-type work is field mapping
only:

| source_type | required config | field mapping |
|---|---|---|
| `shopify` | `shop_domain` (+ optional `api_version`) | order `id` → `external_order_id`; `line_items` → `lines` (sku/qty/price→cents); shop/location → `store_id` resolve |
| `csv` | `column_map` (+ `delimiter`, `encoding`) | header/column map → contract fields (member/order/event/identity kinds) |
| `excel` | `column_map` (+ `sheet`, `header_row`) | same as csv after sheet/header selection |
| `delta_lake` | `table` (+ catalog/path) | table scan → column map → contract fields; checkpoint in `connector_sync_runs` |
| `rest_api` | `base_url` (+ pagination/auth refs) | paginated JSON per entity → contract fields |

Validate-on-create (`validate_connector`): unknown type rejected,
reserved `woocommerce` rejected, per-type required keys enforced, and
any credential-shaped config key (`*_secret`, `*_token`, `*_key`,
`*password*`, …) rejected — secrets are never embedded in config.
Sanitize-on-read: `sanitize_for_log` redacts PII + secrets for logs;
quarantine `raw` keeps PII (exact-match reprocessing) with secrets
redacted at write (`redact_secrets`).

Secrets-at-rest DECISION: `connectors.config` = non-secret params only;
credentials live in env/secret-manager, `secret_ref` names the env var
(e.g. `SHOPIFY_TOKEN`). The DB never stores a plaintext secret.
Rotation = update env, no DDL.

Sync-run history DECISION: new `connector_sync_runs`
(connector + entity + status + rows_seen/rows_ok/rows_rejected + error +
scope) — NOT an extension of 015 `pipeline_runs` (different grain:
sync run vs scheduled job; different readers: connector health vs cron
orchestration). `pipeline_runs` stays job-grain, untouched.

Write/trigger API [ADDITIVE] (`services/api/routers/connectors.py` +
`repos/connectors.py`, all `require_admin` — secrets + execution
outrank uploads): `POST /connectors` (201; 409 duplicate
(source_type, name); 422 validation) → `DELETE /connectors/{id}`
(204, runs cascade) → `POST /{id}/sync {entity, scope?}` (202 queued;
422 on disabled = kill-switch, no force) → `POST /{id}/test`
(stored-or-prospective check, no persistence: config-shape +
secret-presence) → `PUT /{id}/secret {value}` (performs the 019
rotation: sets the env var at runtime, touches `updated_at`, no DDL;
422 without `secret_ref`; process-scoped, lost on restart —
secret-manager remains the pilot path). LD2 per-connector schedule +
sync checkpoint (`025_connector_schedule.sql`, [ADDITIVE]):
`schedule_cron` NULL = manual-only, else validated 5-field cron
(`validate_schedule_cron`; range-checked minute/hour/dom/month/dow);
`PUT /connectors/{id}/schedule` set/clear (404
`connector_not_found`, 422 bad cron). `last_sync_at`/`last_sync_status`
(`queued|running|succeeded|failed`) stamped by the sync worker via
`note_sync_result(run, status, counts)` — closes the run row AND the
connector checkpoint in one transaction (raises on bad status/counts
or missing run). Incremental-extract helpers (no I/O): optional
`pagination` config block (`page|cursor|offset`, cursor REQUIRES
`cursor_param` — page-only extracts rejected as a smell; validated at
registration; `default_limit` 1..1000); `since_pushdown` (server-side
`since` first for `shopify|rest_api`, client-side for file scans);
`since_filter` with fail-open dating (undateable rows INCLUDED +
flagged for quarantine — never silently dropped); `should_retry`
(429/5xx/transport only, never 4xx) + `compute_backoff` (base·2ⁿ,
capped); per-run structured `sync_log` lines; `describe_extract`
shape probe for connection checks. NOT owned: the scheduler tick and
the actual extract/load worker — this is vocabulary + checkpoints so
the app-side scheduler has something honest to drive. LM1/LM2
PLANNED (joint with ml, additive migrations AHEAD of their engines):
`btyd_params` (per-scope fitted BTYD params) + `assoc_rules_eval`
(per-batch holdout metrics) — data to land the tables first, ml builds
engines on our grain after. Every response carries
`has_secret` (presence bit) only; values are never stored, logged,
or echoed. CONVERGED (app ruling 2026-10-15): `has_secret` means
env-live everywhere (can this connector authenticate right now?) —
reads compute it Python-side from the stored `secret_ref` name, same
as writes; 404 code aligned on `connector_not_found`. No migration
(019 covers the tables). VENDORED COPY (2026-10-15): app mirrors
`pipelines/ingest/connectors.py` (+ `__init__` chain) into its tree
for serving; the data copy stays canonical — registry/validation
changes here need an explicit re-mirror, app never edits its copy.

## 020: cohort retention snapshots (D2, [ADDITIVE] analytics)

`020_cohort_snapshots.sql` + `COHORT_SNAPSHOT_UPSERT_SQL` /
`COHORT_TREND_SQL` / `retention_rate` in `pipelines/snapshot.py`.
Grain (agreed with app): one row per (`granularity` x `cohort_period`
x `period_offset` x scope) with `cohort_size` + `retained` +
`retention_rate`. App serves `GET /analytics/cohort?granularity=
week|month|year` + scope filter from this ONE table (retention line +
heatmap). Math: cohort of a customer = `date_trunc(granularity,` first
completed order); `retained(k)` = cohort members with ≥1 completed
order in period (`cohort_period` + k·granularity); `retention_rate` =
`retained`/`cohort_size` (NULL when empty; offset 0 is 1.0 by
construction). Week periods start Monday. Writer re-run = no-op via
the grain UNIQUE. No ML needed (cohort is a data-owned agg). Weekly
cadence rides the scheduler (`cohort_snapshot` engine, same job family
as the forecast snapshot). Scheduler also carries argv builders for
`assoc_batch` / `explain_batch` / `forecast_snapshot` / `matrix_batch`
(ml CLI contracts verbatim); `explain_batch` and `matrix_batch`
manifests store `segment_run_id` (concrete UUID, `latest`, a missing
key, or the all-zero UUID — every form resolved at dispatch to the
newest segment run for `segment_mode` +/- `segment_k` + scope, so
manifests never hold a stale fixed id).

## 021: SHAP cluster-explainer result tables (M2, [ADDITIVE] analytics)

`021_explain.sql`: `explain_batches` (segment_run_id FK →
`segment_runs` cascade + mode/k/seed/top_n/counts/scope) +
`explain_rows` (top-N per batch+cluster by mean|SHAP|: rank/feature/
mean_abs_shap/mean_shap/min_shap/max_shap, UNIQUE against
double-write). PNGs stay MLflow-only; app serves latest-run summaries.
Shape mirrors `ml_engine/store_explain.py` inserts.

## 022: demand-forecast snapshot tables (M3, [ADDITIVE] analytics)

`022_forecast.sql`: `forecast_batches` (as_of/method/horizon_days/
lookback_weeks/category_count/skipped_count/scope, grain UNIQUE for
idempotent weekly cadence) + `forecast_rows` (one row per batch x
category x Monday-week `period_start`: predicted_qty + 80% interval
lower_qty/upper_qty, CHECK-ordered, UNIQUE against double-write).
Method `trend_seasonal_naive`; thin categories counted in
`skipped_count`, never silent. Shape mirrors
`ml_engine/store_forecast.py` inserts. M4 simulator needs no migration
(pure in-process engine, no DB).

## 026: report definitions registry (LD11, [ADDITIVE] analytics)

`026_report_definitions.sql` + `pipelines/reports.py`. Report pages
are app-served, but their DEFINITIONS live here so snapshots and
pages can't drift: `report_definitions(report_id, version, name,
kind, grain, definition jsonb)` pins (axes, metrics, filters) per
report version. Immutable rows (`REVOKE UPDATE/DELETE`, same
convention as 013): a new version is a new row (`ON CONFLICT
(report_id, version) DO NOTHING`), readers take MAX(version) via
`LATEST_DEFINITIONS_SQL`. `validate_report_definition` requires
`report_id/name/kind/grain` + `version >= 1` + at least one of
`axes|metrics|filters` in `definition`. Any RFM/design writeup rides
inside `definition` under `rfm_note` — deliberately no second table.

## 027: landing receipts (LD12, [ADDITIVE] ingest)

`027_landing_receipts.sql` + `pipelines/receipts.py`. One receipt per
landed batch: file uploads (`upload_job_id` FK, UNIQUE — one receipt
per job, `ON DELETE RESTRICT` so landed proof outlives nothing) OR
vendor batch imports (`vendor_batch_id` external key) — CHECK exactly
one of the two. Counts reconcile (`loaded + quarantined <= seen` in
both validation and DDL); `contract` names the contracts.md section
the batch validated against; `manifest_pipeline` links
`pipeline_manifests(pipeline)` — NULL for manual/ad-hoc loads, FK
enforced otherwise. Immutable (`REVOKE UPDATE/DELETE`, same
convention as 013/026). `validate_receipt` is the writer-side gate;
`RECEIPTS_FOR_JOB_SQL` serves the per-job proof.

## 028: fitted BTYD params (LM1, [ADDITIVE] analytics, joint with ml)

`028_btyd_params.sql` + `validate_btyd_params` /
`LATEST_BTYD_PARAMS_SQL` in `pipelines/model_fits.py`. Data lands the
table FIRST; ml's fitting engine writes rows matching this grain. One
row per (as_of x activity x horizon_days x scope x model): `params`
`{r, alpha, a, b}` all > 0, `n_customers`, `loglikelihood`, or
`fit_error` when the fit fails (CHECK: params or error, never
neither). MODEL STRINGS (034, post-repoint fallout 2026-10-15):
canonical values are UNDERSCORE `bg_nbd`/`pareto_nbd` — 028 shipped
hyphen `bg-nbd` but every production-shaped write bounced on the
CHECK while ml's writer+readers were already underscore-consistent,
so data moved (CHECK swap, no backfill — no live hyphen rows could
exist). `validate_btyd_params` rejects hyphen. Grain mirrors
`churn_snapshots` (013) NULL-safe (COALESCE
sentinel UNIQUE) so the serving read joins scope-to-scope; refit =
new `as_of` row, never UPDATE (REVOKE, same convention as 013).
`LATEST_BTYD_PARAMS_SQL` serves the newest successful fit per grain
(failed fits stored but invisible to serving) — this is the read
behind alive_path / prob_n / expected-purchases. ml: conform your
fit writer to these columns; request changes while still additive.

## 029: cluster-vs-population matrix (LM2, [ADDITIVE] analytics, joint with ml)

`029_cluster_matrix.sql` + `validate_matrix_row` /
`MATRIX_FOR_RUN_SQL` in `pipelines/model_fits.py`. CORRECTION
(2026-10-15, pre-push, never applied live): this number first held
an assoc-holdout-eval table from a misread of the LM2 item — the
agreed LM2 was always the comparison matrix (legacy-steal plan +
ml triage). Replaced, not supplemented. One row per
(segment_run x cluster x feature x layout): cluster mean/median vs
population mean/median + diff + pct_diff (NULL when population
median is 0). `layout` 'wide' (matrix cells) vs 'long' (UNPIVOTED
UI rows). Runs die → matrix dies (CASCADE, same as 021 explain
rows). Immutable (REVOKE UPDATE/DELETE). ml: write per-feature
rows per cluster per run in either layout; serving reads one layout
per run.

## 032: long-layout matrix (LM2, [ADDITIVE] analytics, joint with ml)

`032_matrix_long.sql` + `validate_long_row` /
`LONG_MATRIX_UPSERT_SQL` / `LONG_MATRIX_FOR_RUN_SQL` in
`pipelines/model_fits.py`. Answers ml's grain question: wide's PK
(run, cluster, feature, layout) cannot hold melted stat rows, and
stat-name-in-feature would pollute the parsed feature namespace —
so long gets its own table, no PK surgery on 029. One row per
(run x cluster x feature x stat); stat is one of the six wide value
columns by name (`cluster_mean`, `cluster_median`,
`population_mean`, `population_median`, `diff`, `pct_diff`);
`value` NULL-able (pct_diff NULL on zero population, same rule).
Runs die → long rows die (CASCADE). Immutable
(REVOKE UPDATE/DELETE). ml: `long` layout no longer raises — write
one row per stat per feature per cluster.

### `matrix_batch` engine (runner, [ADDITIVE], joint with ml)

`pipelines/schedule.py` registers engine `matrix_batch` alongside
`explain_batch` (`ENGINE_BUILDERS`); both dispatch through the shared
`_resolve_segment_run_id`, so a manifest's `segment_run_id`
(concrete / `latest` / missing / all-zero) becomes the newest
`segment_runs` row for (`segment_mode`, optional `segment_k`, scope)
at dispatch. No matching run = job `failed` with an actionable
ManifestError and downstream jobs `skipped`.

CLI (ml, `ml_jobs/run_matrix_batch.py`): `python
ml_jobs/run_matrix_batch.py DB_URL --segment-run-id UUID`. Argparse
declares nothing else — the runner passes no `--as-of`, no
`--brand-id`/`--group-id`, and (unlike `explain_batch`) no
`--created-by`; a flag the CLI does not declare is an exit-2 on every
tick. Exit codes are a signal (taxonomy confirmed by ml): 0 = success
(both layouts written); 2 = argparse refusal, which only a drifted argv
builder can produce; 1 = a genuine runtime failure -- unreachable DB, a
`segment_run_id` the writer rejects, or any exception inside
`store_matrix` (a non-UUID id lands there too, `uuid.UUID()` raising
before any DB work). The resolver only ever hands over a concrete UUID,
so rc=1 with a resolver-produced id is precisely the resolver/writer
disagreement worth pinging ml about, while rc=2 from the tick is a
contract drift on the data side. One invocation writes BOTH layouts
(029 wide + 032 long).
Weekly placement: immediately after the segments run it reads,
`depends_on: [segments_overall]` (mirrors
`ml_jobs/pipelines.example.yaml`). Manifest rows stay edited through
the app config API, never migrations (015 rule), so the live weekly
row gains this job there; manual invocation needs no manifest change.

Guard (honest refusal, not a silent empty table): the job rejects
non-centroid backends (`dbscan`/`hdbscan`) with an unprocessable
error, same as the explain path — if `segments_overall` were ever
configured with dbscan the matrix stays dark by design and the tick
records the refusal on its `pipeline_runs` row.

## 030: surrogate-fit registry + explain method labels (LM1, [ADDITIVE] analytics, joint with ml)

`030_surrogate_fits.sql` + `validate_surrogate_fit` in
`pipelines/model_fits.py`. Per the ACKed LM1 method spec: one fit
row per segment run (`seg_surrogate_fits`: segment_run_id UNIQUE FK
CASCADE + `trained_at` + `accuracy` 0..1 + `feature_names` +
`xgb_params`); accuracy lives on the fit row ONLY. `explain_batches`
gains `fit_id` (NULL for mean-method rows, SET NULL if the fit row
goes); batches + rows gain `method` (`segment_mean` (all history —
the default, zero backfill rewrite) | `xgb_surrogate_v1` (gate
passed) | `surrogate_unfitted` (gate failed, NO shap rows)).
`validate_explain_write` (merge `844b770`, NOT the label-only
`validate_explain_method`) is the writer-side gate: unfitted +
shap_rows>0 raises; v1 requires a fit_id AND accuracy >= 0.90
(passed explicitly, read back from the fit row). No tick writes
under `xgb_surrogate_v1` until spec ACK — ACKED with this
migration; ml may write.

## 031: time-aware RFM storage (LD8, [ADDITIVE] analytics, joint with ml)

`031_rfm_time_aware.sql` + validators/SQL in `pipelines/snapshot.py`.
Per the ACKed LD8 joint contract: per-window snapshots reuse
`rfm_snapshots`/`rfm_customer_scores` + a `window_key` label
(`current` rolling-365/LD5-as-of | `mom` prev calendar month |
`yoy` same month prior year | `lifetime`); `rules_version` 'v2' on
time-aware rows (column pre-existed, default 'v1' — v1 history
untouched). New: `rfm_segment_sizes` (as_of x window_key x
segment: n + median_r/f/m; diff/pct vs comparator served by
query) and `rfm_segment_movement` (as_of x pair x from x to:
`current_x_mom`|`current_x_yoy`, n + pct_of_from). ENTERED (only
as from) / EXITED (only as to) sentinels for newcomers/exited —
uppercase, never collide with lowercase segments; pct_of_from NULL
enforced (outside the flow pct denominator by construction).
Lesson: `window` is a PG reserved word — column is `window_key`.
ml executes the movement math verbatim via `validate_movement_row`
/ `validate_segment_size` + the UPSERT/SELECT SQL.
