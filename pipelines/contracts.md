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
<name> --ml-repo <path>` (cron entrypoint). Manifest parsed from the
table (`--manifest-file` dev override never writes). Topo order over
depends_on (cycle = error); data engines in-process (upload_apply =
sweep validated jobs, kpi_daily = KPI upsert); ml engines as
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
secret-manager remains the pilot path). Every response carries
`has_secret` (presence bit) only; values are never stored, logged,
or echoed. No migration (019 covers the tables).
