# Source → table contracts

How to cite here: `pipelines/citation-rules.md` — the rules that make a cite checkable.
After editing this file, run `python scripts/audit_doc_cites.py`.

## Contents

- § customers
- § orders
- § events / consents
- § Failure handling
- § Snapshot history (serves downstream trends + training)
- § Daily RFM writer: `ld8_snapshot` (v2) vs `rfm_snapshot` (v1)
- § Batch upload ETL (frontend files land here)
- § 015: segments + scheduled pipelines (ml part 2, data-owned tables)
- § 016: modeling-upgrade flag mapping (ml registry / algorithms / signals)
- § 019: source connector framework (D1, [BREAKING ingest])
- § 020: cohort retention snapshots (D2, [ADDITIVE analytics])
- § 053: recommendation attribution (D3, [BREAKING ingest])
- § 021: SHAP cluster-explainer result tables (M2, [ADDITIVE analytics])
- § 040: SHAP batch coverage (`clusters_explained`, `clusters_skipped`)
- § 022: demand-forecast snapshot tables (M3, [ADDITIVE analytics])
- § 026: report definitions registry (LD11, [ADDITIVE analytics])
- § 027: landing receipts (LD12, [ADDITIVE ingest])
- § 028: fitted BTYD params (LM1, [ADDITIVE analytics], joint with ml)
- § 029: cluster-vs-population matrix (LM2, [ADDITIVE analytics], joint with ml)
- § 032: long-layout matrix (LM2, [ADDITIVE analytics], joint with ml)
- § 030: surrogate-fit registry + explain method labels (LM1, [ADDITIVE analytics], joint with ml)
- § 031: time-aware RFM storage (LD8, [ADDITIVE analytics], joint with ml)
- § 037: backtest outcomes (M2, [ADDITIVE analytics], joint with ml)
- § The stamped set: which relations carry a scope stamp (app+data+ml, 2026-10-17)
- § The group filters and their populations (2026-10-22)
- § Tree ownership & the mirror boundary (manager ruling 2026-10-22)
- § `pipeline_runs`: what a reader may rely on (2026-10-20)
- § The expectation floor: what "no row" means per job (2026-10-20)
- § The customer list's value and risk: one snapshot, one row per customer (2026-10-21)
- § The churn `all` leg is a nightly job, not a tab (2026-10-21)

## customers

| source field | table column | rule |
|---|---|---|
| member code | `member_id` | must match `^M-[0-9]{6}$`, else reject row + quarantine |
| source key | `external_member_id` | unique per source; second-see is update, not insert |
| re-ingest of a soft-deleted member | `deleted_at` | **revive**: a re-ingested member clears the tombstone (the feed is authoritative for "this member exists again") |
| re-ingest of a soft-deleted member | `deleted_at` | a revived customer is counted as `customers_revived` in the run summary and written as the staged row's reason |
| re-ingest of a soft-deleted member | `deleted_at` | a resurrected customer reappears in every segment and dashboard |
| phone/email | `phone`, `email` | exact-first matching |
| phone/email | `phone`, `email` | on an exact miss a bounded substring fallback (min 3 digits / 3 chars, LIMIT 20, POST body, criteria-type-only audit) |

## orders

| source field | table column | rule |
|---|---|---|
| store code | `store_id` | NOT NULL; resolve via store map, create store if whitelisted |
| source order key | `external_order_id` | idempotency key; replay = no-op |
| demo seed | `source='demo'`, `external_order_id='DEMO-O-NNN'` | stable keys, so re-seeding `db/seeds/demo.sql` inserts nothing |
| demo seed | `source='demo'`, `external_order_id='DEMO-O-NNN'` | the item rows are gated on the order having no lines |
| line amounts | `order_lines` | integer cents; `total_cents = Σ qty × unit_cents` |
| order total | `total_cents` (feed) | optional; when present it must equal `Σ qty × unit_cents` |
| order total | `total_cents` (feed) | a discount, shipping or tax line is exactly the mismatch shape, so a disagreement quarantines the row with both numbers rather than silently picking one |
| order total | `total_cents` (feed) | with no feed total the line sum is stored |
| timestamp | `ordered_at` (raw) | optional, ISO-8601 |
| timestamp | `ordered_at` (raw) | unparseable, or tz-naive with no feed zone, **quarantines** (never `now()`, which files a bad row under today's revenue) |
| timestamp | `ordered_at` (raw) | a naive value is read in the feed zone (+08), not UTC |

## events / consents

Append-only.
Dedup key `(source, source_event_id)`.
The **upload job's** source is the one written, so a row whose payload carries a different `source` is rejected.
Consumers (app timeline, ML features) read; nobody updates.

### kind vocabulary (additive; new kinds need ml ack before writing)

- Manual kinds (source `manual`, written by app): `note`, `visit`, `call`, `complaint`, `follow_up`.
- Derived kinds (source `derived`, written by data): `order_placed` — one row per order, `source_event_id = 'order:' || order_id`, `occurred_at = orders.ordered_at`, payload `{order_id, total_cents, store_id}`.
- ML calibration EXCLUDES `order_placed` by name (`store_churn` activity calibration filters `ce.kind <> 'order_placed'`) so derived rows can never double-count orders under activity='all'.
- Renaming the derived kind, or adding any further derived kind, requires ml ack FIRST — the exclusion is name-keyed.

## Failure handling

Rejects go to quarantine with (source, key, reason, raw, `code`) — never silently dropped.
`code` is a stable token from `pipelines/ingest/errors.py` (`RowRejected` / `RowMissing`, read back by `code_of`).
The job carries `quarantine_reasons` = `{code: count}` (`043_quarantine_codes.sql`, `up.reason_counts`); `{}` on a clean run, asserted in `services/api/tests/test_apply.py`.
One code is a data rule rather than a parse failure: `future_dated` quarantines a `status='completed'` order whose `ordered_at` is after `now()`.
A future *scheduled* row is a different fact and is applied.
The row is dead but the data is not: 014 revoked UPDATE/DELETE and 043 grants UPDATE on `resolved_at` alone, so the record is never edited or deleted.
Re-applying the same `(source, key)` after the feed is fixed applies the row and stamps `resolved_at` on the open record (`RESOLVE_QUARANTINE_SQL`) — an append-only trail of the reject and of its resolution.
A revived customer's derivations are **carried over, not recomputed** (measured: churn 13 / clv 15 / rfm 13 / segment 24 rows identical before the soft delete and after the revive, `max(created_at)` unmoved), so the scores describe the pre-deletion state until the next scheduled run.
While the customer is deleted its rows do not resolve, so new orders/events for that member quarantine as `customer_unresolved` instead of landing.

## Snapshot history (serves downstream trends + training)

RFM snapshots (`rfm_snapshots`) + rec batches (`rec_batches`) are the existing persisted runs.
`013_analytics_history.sql` adds the missing history so movement / pct-change / training lineage work across days:

| run table | grain | idempotency |
|---|---|---|
| `clv_snapshots` + `clv_customer_scores` | one run per `as_of` x horizon x scope | UNIQUE `(as_of, horizon_days, scope)` |
| `churn_snapshots` + `churn_customer_scores` | one run per `as_of` x activity x horizon x scope | UNIQUE `(as_of, activity, horizon_days, scope)` |
| `kpi_daily_snapshots` | ONE ROW per day x scope (totals + day deltas + active_30d) | UNIQUE `(snapshot_date, scope)`; re-run = no-op |
| `model_training_runs` | which snapshot/params trained which model | append-only log |

Math lives in `pipelines/snapshot.py` (`pct_change`, `kpi_movement`, `clv_movement` + `KPI_DAILY_UPSERT_SQL` / `KPI_TREND_SQL`).
Daily KPI job: `python scripts/run_daily_snapshot.py --as-of YYYY-MM-DD` — it writes **one `kpi_daily_snapshots` row and nothing else**.
The ML `run_snapshot` / `run_recs_batch` lines in its docstring are sibling invocations to run separately (then log to `model_training_runs`).
The script never writes `rfm_snapshots`.
Model runs are scheduler manifest jobs — see **Daily RFM writer** below.
Queries in `db/queries/kpi_daily.sql`.
Scope columns (`brand_id`, `group_id`, NULL = not group-scoped) match 012.

**Snapshot day boundary is business time (Asia/Hong_Kong, +08).**

- `as_of` / `window_from` / `window_to` are business-day dates: a bound named `2026-10-16` is `2026-10-16T00:00+08` = `2026-10-15T16:00Z`.
- `rfm_snapshots.window_from` is a `date`, so no stored row reveals the offset; a reader assuming `SHOW TimeZone` (GMT on this stack) or the host clock is wrong by up to 8h.
- ML pins the naive->aware conversion (`ml_engine/rfm.py:_day_start`, `ZoneInfo("Asia/Hong_Kong")`); zero stored values change on a +08 host, so no `RULES_VERSION` bump.
- Fixed 2026-10-17 in `pipelines/snapshot.py` (no migration, no schema change): every bucket-deciding trunc carries the offset (`BUSINESS_TZ_SQL`, `AT TIME ZONE interval '{BUSINESS_TZ_OFFSET_HOURS} hours'`).
- `business_today()` (`pipelines/snapshot.py:245`) = `datetime.now(timezone(timedelta(hours=8))).date()` is the default `--as-of` of `scripts/run_daily_snapshot.py:70` and `scripts/run_pipeline.py:144`.
- `BUSINESS_TZ_NAME` keeps the zone name (app pins `TIME_ZONE` in `apps/web/lib/format.ts`, ML pins `ZoneInfo`).
- The defects: `date_trunc($1::text, ordered_at)` ran in the **session** zone (GMT), so week/month cohorts were UTC — over 8,685 orders, 137 month / 628 week / 3,698 day shift; and the `current` cutoff was server `CURRENT_DATE - 1` while both scripts defaulted `--as-of` to host `date.today()` (they disagree 00:00-08:00 local).
- **Cohort figures change by design; old and new are NOT comparable.** Over 940 purchasing customers, 10 month / 62 week / 0 year cohorts move.
- `cohort_snapshots` was purged and regenerated: its grain UNIQUE (`granularity, cohort_period, period_offset, COALESCE(brand_id), COALESCE(group_id)`, `020_cohort_snapshots.sql:31`) has **no tz component**, so a re-run without the purge would mix UTC and +08 generations in the heatmap.
- Post-fix: offset-0 `cohort_size` == +08 recomputation for all 36 month / 152 week / 4 year buckets, 940 members each (that 940 is the demo scope **at the 2026-10-18 measurement** — the scope grows with every demo tick, so the equality is the claim and the count is a snapshot).
- Server-zone exception, one CHECK: every stored date is passed as an explicit `$1::date` parameter, except `db/migrations/017_customers_demographics.sql:11 CHECK (date_of_birth IS NULL OR date_of_birth <= CURRENT_DATE)`, evaluated in the **server** zone (a today DOB is then a violation 00:00-08:00 HKT).
- Deliberately not tightened — a false rejection of a newborn's DOB is worse than a one-day-future value.
- Environment trap: this pgembed install ships **no tzdata**, so `AT TIME ZONE 'Asia/Hong_Kong'` raises `time zone "Asia/Hong_Kong" not recognized`; any SQL-side pin must use the explicit offset form (Hong Kong has no DST).

**A KPI row's cumulative columns are the state as of that row's own day.**

- `customers_total`, `orders_total` and `revenue_cents_total` are bounded by the row's own `d_to` (the next business day's midnight), so an order dated after it is not yet part of that day's world.
- Unbounded, a row carried the present table's counts under a past date's label: the manager measured it on the live cluster — the row labelled `2026-10-04` carried `orders_total=8689` while the present was `8686`.
- After the reseed on that cluster the three global rows read 8383 / 8458 / 8523 for 2026-09-30 / 2026-10-04 / 2026-10-08 — a curve that rises, each row the count of the orders before its own day.
- The rule has one home: `pipelines/snapshot.py` `KPI_DAILY_UPSERT_SQL` (its scope CTEs are `KPI_DAILY_CTES`, which the reseed reuses).
- The reseed order: the fix ships first (a reseed against the old writer recomputes the old numbers), then `scripts/reseed_kpi_cumulative.py` walks the rows in ascending `snapshot_date` and rewrites each through the writer's own statement, then `ANALYZE kpi_daily_snapshots`.
- The order decides only which rows are corrected if the run is interrupted: each row is bounded by its own day, so its value does not depend on the order.
- The pin: `services/api/tests/test_snapshots.py::test_kpi_cumulative_columns_are_as_of_the_row_date`; its teeth are `scripts/mutation_check_kpi_cumulative.py` (three mutations, one per cumulative column).

**Which `as_of` default do you get?** Read the call site, not the column.

| source (default when `as_of` is omitted) | resolves to |
|---|---|
| data `pipelines/snapshot.py:245 business_today()` (+08 fixed offset), used by `scripts/run_daily_snapshot.py:70`, `scripts/run_pipeline.py:144` | +08 business date |
| data scheduler `pipelines/schedule.py:430` in `_with_common` | passes `--as-of ctx.as_of`; `explain_batch` / `assoc_batch` / `matrix_batch` / `recs_batch` declare no `--as-of` (argparse rejects it, `pipelines/schedule.py:568`) — always explicit or absent |
| ml `ml_engine/rfm.py:79 business_today()` (`ZoneInfo("Asia/Hong_Kong")`), applied by `ml_engine/rfm.py:123` in `resolve_window(...)` (`:101` def), `ml_engine/store_ld8.py:240 run_ld8(...)`, `_coerce_as_of` of `store_churn.py:217`, `store_clv.py:189`, `store_forecast.py:50`, `store_segments.py:419` | +08 business date |

Guard `tests/test_engine.py:382-408` pins an in-band instant 17:37Z = 01:37 HKT.

**`--as-of` labels a run; it does not anchor a fit.**

The OVERALL/current segments fit is always the current rolling-365 business window (`_fit_overall`, `ml_engine/store_segments.py:174-181`; same at `store_explain.py:97-98`), so a run row's `as_of` is an idempotency/version key, not a window key.
For a backfill only `forecast` (`as_of` → `forecast_demand`, `store_forecast.py:80-81`) and `ld8` (`resolve_ld8_window(key, as_of)`) derive a window from `--as-of`.
churn/clv fit against `datetime.now(timezone.utc)` and segments-overall fits the current window, so re-running an old `as_of` there **relabels** today's fit.

**Name collision — `resolve_window` is two different functions.**

data `pipelines/schedule.py:173 resolve_window(mode, n, unit, ...)` = cohort/period window.
ml `ml_engine/rfm.py:96 resolve_window(mode, as_of, date_from, date_to, *, max_span_days=731)` = RFM snapshot window (tz-aware `+08`; `max_span_days=None` is the caller asserting a deliberate full-history span, not a clamp).
Check the repo before reading either as the other's contract.

## Daily RFM writer: `ld8_snapshot` (v2) vs `rfm_snapshot` (v1)

`rfm_snapshots` has two *scheduled* writers, and the engine name in the manifest decides which one a tick uses (a third, non-scheduled writer is at the end of this section).

| engine | ml job -> module | rules | windows | conflict behaviour |
|---|---|---|---|---|
| `rfm_snapshot` | `ml_jobs.run_snapshot` -> `ml_engine/store_rfm.py` | **v1** (`ml_engine/rfm.py:24 RULES_VERSION = "v1"`) | one, from `params.mode` | **APPEND-only**: `SNAPSHOT_INSERT_SQL` has no `ON CONFLICT` and no find |
| `ld8_snapshot` | `ml_jobs.run_ld8_snapshot` -> `ml_engine/store_ld8.py` | **v2** (`store_ld8.py:36 RULES_V2`) | FOUR: `current` (rolling 365d ending as-of) \| `mom` \| `yoy` \| `lifetime` | find-or-create on (mode, window_from, window_to, window_key, scope), filtered `rules_version='v2'` |

- 031 declares v2 for time-aware rows, so **v2 is canonical for `window_key='current'`** and `ld8_snapshot` is the daily-capture engine; `rfm_snapshot` stays for v1 history/backfill only.
- Trap: if the daily job is left on `rfm_snapshot`, the v1 insert is append-only and the v2 find is v2-scoped, so one v1 and one v2 row land for the same `window_key='current'` window **every day**; `GET /analytics/rfm/movement?mode=rolling_365` auto-selects the two most recent rows by `window_to` and only then rejects a version mismatch, so the default path answers **422 `snapshot_mode_mismatch`**.

**Never pair v1 with v2 for movement.**

Same maths both ways (`store_ld8.compute_window` calls `store_rfm.compute_current` verbatim, `ml_engine/store_ld8.py:141-175`), but v1 is an append-only single-window write and v2 a four-window find-or-create, and the *engine dict* decides the version, not the caller.
The v1 INSERT takes `rules_version` as a parameter (`SNAPSHOT_INSERT_SQL`, `store_rfm.py:35`; column at `:37`, `computed["rules_version"]` passed at `:179`) — `compute_current` reads the constant at `:153` (`"v1"` at `rfm.py:24`) — while the v2 INSERT interpolates the literal `'{RULES_V2}'` (`SNAPSHOT_INSERT_V2_SQL`, `store_ld8.py:52`, VALUES at `:56`; find twin `SNAPSHOT_FIND_V2_SQL` at `:61`, WHERE at `:64`; `RULES_V2 = "v2"` at `:36`), so cross-version movement is meaningless.
Serving fix: take the newest snapshot's `rules_version`, then the two newest **of that version** for that `mode`/`window_key` in scope, returning the existing `need_two_snapshots` 404 when only one exists — never relax the mismatch check.
- Pairing is **same `mode` + same `rules_version` + same `window_key`**; the bounds need not match and normally do not (each tick find-or-creates its own window, so a same-day re-run adds nothing).
- `window_key` separates windows *within* a mode (`current` vs `mom`/`yoy`/`lifetime`) and never pairs across them: a two-row requirement is always **two nights of the same window**.
- The version term is load-bearing for `rolling_365` (a v1 `current` row sits beside v2 `current` rows) and vacuous in `custom` (no v1 row), because `window_key` **cannot** separate v1 from v2: it is `NOT NULL DEFAULT 'current'` (`db/migrations/031_rfm_time_aware.sql:19-20`) and the v1 INSERT omits the column (`store_rfm.py:35-43`, nine columns at `:37-38`), so the **DDL default fills it** and a v1 `rolling_365` row reads `'current'` exactly like a v2 one.
- **LD8's reachable space is exactly four combinations** (`ml_engine/store_ld8.py`: `WINDOW_KEYS` is the four keys, and `compute_window` maps each to its mode in the if/elif/else at `:153-169` — that function's docstring is the write-site invariant): `rolling_365×current`, `custom×lifetime`, `custom×mom`, `custom×yoy` — **`custom×current` is unreachable from the writer**.
- The `window_key='current'` pin therefore yields the empty state structurally.

**Switching the job is a manifest edit, and `pipeline_manifests` rows are edited through the app config API, never by migration** (015 seed comment):

    rfm:
      engine: ld8_snapshot
      depends_on: [apply]
      params: {}          # drop `mode: rolling_365`: the LD8 CLI has no --mode

The builder rejects the legacy param with a named `ManifestError` rather than letting the CLI exit 2 on every tick (`pipelines/schedule.py` `_build_ld8`).

**First-tick triage.**

The argv's positional DB URL is the runner's resolved `ctx.db_url` (`pipelines/schedule.py:426` emits `[python, -m, <module>, ctx.db_url]`, sourced from `scripts/run_pipeline.py:106-127` via `_connect_resolved`), **never** a manifest `params` value.
A wrong row fails at dispatch as a `ManifestError` with a message *above* the spawn; a bad URL is a runner/env fault that takes every ml job in the tick down; an unset one is caught pre-spawn (`pipelines/schedule.py:420-425`, `UnknownEngineError`).
The ml CLI validates its args *before* connecting (`ml_jobs/run_ld8_snapshot.py:20-28`; order-only change, ml `148cced`), so a malformed `--as-of` or scope UUID fails as a `ValueError` with no connect attempted.
A bad URL is a driver error (`exit 1`, e.g. `socket.gaierror` / `ConnectionRefusedError`) that reads as a connectivity incident.
The tick is unaffected either way since it always passes `ctx.as_of.isoformat()` (`pipelines/schedule.py:430`, a `date`).

**`--as-of` is a silent no-op knob on a window that already exists.**

On the tick it is always explicit (`_build_ld8` → `_with_common`, `pipelines/schedule.py:469`/`:402-410`).
`ld8_snapshot` find-or-creates on `(mode, window_from, window_to, v2, window_key, scope)` (`ml_engine/store_ld8.py:63-66`, the `SNAPSHOT_FIND_V2_SQL` predicate).
So an `--as-of` whose `current` window is already stored (e.g. `--as-of 2026-10-16` while the v2 row for `2025-10-16 → 2026-10-16` exists) exits 0 having written nothing: the tick is green, no row is wrong, and a consumer needing a newer period stays on `need_two_snapshots` with nothing to point at.
Rule: omit `--as-of` unless you intend a new period; never reuse a date whose window is on disk.

**Serving-side freshness.**

The app's `POST /analytics/{churn,clv,segments}/snapshots` calls `store_*.create_snapshot` **in-process with `as_of` omitted** (app `services/api/analytics/snapshots_serve.py:48-54`).
So an `ml_engine/` change is live for `ml_jobs/` immediately but **live in the API only after that process restarts** — compare the changed file's mtime against the serving process's start time, not `git log`.

**`created_by` provenance.**

`--created-by` is passed through (`scripts/run_pipeline.py:340` default `scheduler`), so via the runner a NULL is **unreachable**.
Of the 12 `ml_jobs/run_*.py` entry points, 10 declare `add_argument("--created-by"` and 9 reference a `created_by` column.
The 2 that declare nothing (`run_alive_path.py`, `run_matrix_batch.py`) reference none.
`run_churn_predict.py:50-52` is **inert** — it accepts the flag and never passes it (`:26` calls `store_churn.predict_one`, `:54` prints JSON) — and no tick can reach it.
`churn_predict` is not an `ENGINE_BUILDERS` key (`pipelines/schedule.py:619`, ten engines), and `run_pipeline`'s dispatch (`pipelines/schedule.py:890`, `:898`) refuses an engine with no `ENGINE_BUILDERS` entry instead of skipping it.
app settled the other half: `churn_customer_scores` **is** read by app (`db/queries/analytics_customer.sql:26-33`, `analytics_snapshots.sql:31`), but those rows come from `store_churn.create_snapshot` (`:478`; `SNAPSHOT_INSERT_SQL` `:538`, `SCORES_INSERT_SQL` via `executemany` `:548`) driven by the churn snapshot writer `ml_jobs/run_churn_snapshot.py:23`, while `predict_one` (`:411`) writes nothing (`return out` at `:428`/`:475`) — so removing the dead flag is app-safe as well as scheduler-safe.
A NULL in the column therefore dates the row to a **direct ml-CLI hand-run** of one of the 9 writers, or to a tick handed an explicitly falsy stamp: `--created-by ""` is dropped by the truthiness guard at `pipelines/schedule.py:416-417` (again at `:451-452`) before `scripts/run_pipeline.py:340`'s non-empty default applies.
`JobCtx` is constructed only at `scripts/run_pipeline.py:124`.

**Pass a staff code wherever a job writes.**

- The hole is **structural, not incidental**: all 12 `created_by` columns in this repo's DDL are `text NULL` with no `DEFAULT` (`db/migrations/007_customer_events.sql:14`, `010_analytics_rfm.sql:15`, `011_recommendations.sql:14`, `013_analytics_history.sql:24,57,115`, `014_upload_staging.sql:39`, `015_segments_pipelines.sql:37`, `018_assoc_rules.sql:25`, `019_connectors.sql:27`, `021_explain.sql:27`, `022_forecast.sql:30`), so an unstamped hand-run **cannot fail anywhere**.
- Data's own CLI follows the convention (`scripts/run_upload.py:130` `default=None`; the API stamps the session, `services/api/routers/customer_events.py:73`).
- Making `--created-by` **required** in the 10 ml CLIs was considered and **declined**: a 10-file argv change (`[BREAKING @both]`) that forces every dev/one-off invocation to pass a value, so the rule above closes the hole operationally.

**The list query's window projection.**

`SNAPSHOTS_LIST_SQL` (`store_rfm.py:53-66`) projects `window_key` (`:54-55`) and orders `window_to DESC, scored_at DESC, id DESC` (`:64`) — period, not compute time.
App consumes it **directly** (`../crm-cdp-app/services/api/routers/analytics_rfm.py:37`), and its `custom` tab pairs the two newest `custom` rows.
Trap: `mode='custom'` in `rfm_snapshots` is **LD8's named-window bucket** (`mom`/`yoy`/`lifetime`), not the user's custom date range — the tab's *scores* come from a live computation over the requested dates, its *band* from that bucket (the stored `custom` rows are all v2: `lifetime` `807d4f38` / `2895ba93`, `mom` `880c0cbe`, `yoy` `8950aaec`).
What is wrong there is **label vs window**: the band prints run dates, not bounds (`formatDate(movement.from.scored_at) → formatDate(movement.to.scored_at)`, `apps/web/app/rfm/page.tsx:501`, also `:551-552` — a segment-chart *axis* — and `:624`), so a bounds-based fix is a three-site edit and a chart-semantics change, not a one-liner.

**Successive-period rule, branched by what the page displays.**

A *mode-defined* window (`rolling_365`, `year_to_date`) tests **translation** — equal deltas at both ends equal to the cadence (the `rolling_365` pair is 1d/1d, accepted; the `custom` `lifetime` pair grows from the 2023-11-12 floor, 0d/1d, rejected because it *grows* rather than shifts).
An *explicit range* (the custom tab's `date_from`/`date_to`) tests **equality with the request** — identical bounds, different `scored_at`; translation alone accepts any two same-length ranges in different months.
The `window_key='current'` pin stays in both branches, yielding the empty state structurally.

**POST path.**

`SNAPSHOT_INSERT_SQL` (`store_rfm.py:35-43`) omits `window_key` from its column list (`:37-38`, nine columns), so the DDL default stamps `'current'` on every app-POSTed row **regardless of mode or user dates**.
The `RETURNING` names it too (`:40-42`), so a POSTed row comes back **self-identifying** (`mode='custom'` *and* `window_key='current'`) with no schema change and no extra request, and two POSTed `custom` snapshots for different ranges would both be `current`.
`window_key`'s job here is in the **guard**, not the predicate: the movement guard checks `mode` + `rules_version` only (`analytics_rfm.py:265-270`), so a cross-key pair is accepted with 200 — proven live `from=880c0cbe` (`mom`) → `to=807d4f38` (`lifetime`) → `customers_both 252, entered 688, exited 0`, which is the window widening, not movement.
Scope that fix to this endpoint: LD8's own `segment_movement` pairs different keys **by design** (`mom`→`current`; `ml_engine/segment_movement.py:23-26` `PAIR_WINDOWS`).
- `store_rfm.py:36` and `store_ld8.py:53` are the only production writes of `rules_version` (the app POST reuses the former; its only direct INSERTs are in `../crm-cdp-app/services/api/tests/test_rfm.py:451/513/537`), so a scope holding **only v1 rows** is exactly a scope **LD8 has never been ticked in** — check `SELECT DISTINCT rules_version` for that scope; the remedy is a scoped LD8 run, not an empty-state fix.

**The four `snapshot.py` statements are effectively frozen.**

ml `tests/test_ld8.py::test_031_statements_still_match_data_runner` reads this repo's `pipelines/snapshot.py` (via `DATA_REPO_PATH`, default `../crm-cdp-data`), extracts the four statements by name and asserts **token**-equality — `_norm` collapses every run of whitespace, so reformatting passes and only a token change fails (`tests/test_ld8.py:43`).
It also asserts the whole `*_SQL` name **set** — keyed by *name shape*, not value shape, so no constant form escapes the extractor (`_data_sql_names`, `tests/test_ld8.py:103`; the frozen expectation `_DATA_SQL_NAMES`, `tests/test_ld8.py:124`, eleven names: the four mirrored plus seven data-only, `BUSINESS_TZ_SQL` among them).
So an **addition** fails ml's suite too, naming the missing constant and its source (`tests/test_ld8.py:157`); the skip is the absent sibling checkout (`@pytest.mark.skipif`, `tests/test_ld8.py:139`).
Data's four constants are `pipelines/snapshot.py`'s `SEGMENT_SIZE_UPSERT_SQL`, `MOVEMENT_UPSERT_SQL`, `SIZES_FOR_WINDOW_SQL` and `MOVEMENT_FOR_PAIR_SQL` — the **names are the pin**, and a line number is worse than illustration.
Read at `ac39625`, the four constants' line numbers had all moved and not one byte of them changed — the **names are the pin**, so this file quotes none.
**data's** `crm-cdp-data/services/api/tests/test_rfm_movement.py:78-81` imports all four and executes them against a migrated `031` (`:123`/`:146`/`:149`/`:157`), so a **grain** change fails data's suite at the change site — and only data's *can*.
ml's `pytest tests` runs without `DATABASE_URL` by design (pure-engine suite), so its reachable half is text-only, and its guard **skips** entirely from a checkout with no sibling `crm-cdp-data` (`tests/test_ld8.py:139`).
A change to these four statements' text or grain is an in-room `[BREAKING]` notice to crm-ml, not a repo-only edit.
App has no equivalent check — `../crm-cdp-app/db/queries/rfm_windows.sql` mirrors the two read statements by hand (app names `sizes_for_window` `:11`, `movement_for_pair` `:17`) and app's `services/api/tests/test_rfm_windows.py` exercises the endpoints over rows it seeds itself (`_seed_windows` `:74`), importing none of the four names.
So app's mirrors lag and fail *worse*: self-contained SQL keeps serving an old shape with no error anywhere, and ml's test compares *data's* statements, not app's.

**`m_cap_pct` is a live-request knob, never a stored property.**

The cap is an engine kwarg (`ml_engine/rfm.py` `assign_scores(...)` declares it, applied `:287-290`; forwards to `store_rfm.py:115/140`, `store_ld8.py:157-174`) reachable from the app's two live GETs only (`../crm-cdp-app/services/api/routers/analytics_rfm.py` params `:115`/`:279`, `Query(ge=0.01, le=1.0)`, passed straight to the engine at `:125`/`:290`).
The scheduled writer always uses `DEFAULT_M_CAP_PCT = 0.99`, and no renderer emits a flag for it (the `m_cap_pct: (False, None)` entry `rfm_snapshot` carried was a declaration nothing could turn into argv; removed).
Do not add a `--m-cap-pct` flag, and do not pin the GETs: `rfm_snapshots` stores no cap and no params (`db/migrations/010_analytics_rfm.sql:6-16`), so a caller-chosen cap row would be unattributable from storage, while a live response **self-attributes**, returning the cap it used (`"m": {"m_cap_pct": …, "winsor_cap_cents": …}`, `ml_engine/rfm.py:317`, validated `:292-295`).
The write path is **pinned by decision** (app, read at `6bdf2b4`; guard `_checked_body` at `analytics_rfm.py:55-67`, applied on the POST at `:140`): the body default is the engine constant (`:52`).
A non-default cap is rejected by name — `422 {"code": "non_default_m_cap_pct", "default": engine.DEFAULT_M_CAP_PCT}` (`:61-64`) — and any cap outside `(0, 1]` with `bad_m_cap_pct` (`:59-60`).
Cap experiments belong on the live endpoints, which persist nothing.
- Edge 1: the cap is consulted **only on the quintile path** — `score_rfm` validates and applies it in the `else` branch alone (`:292-295`, reported `:317`), so a population below `QUINTILE_MIN_POPULATION` (`QUINTILE_MIN_POPULATION`) takes `_fallback_scores` (`:289`) and is stamped `method="threshold_fallback"` (`:290`): the cap is never read, and that path's `m` boundaries are median-relative with no `m_cap_pct` key.
- Edge 1 scope caveat: the cap statement above is therefore honest only on the quintile path — irrelevant at the live population, but it bounds the app refusals too, which guard a parameter the engine may not consult.
- Edge 2: `DEFAULT_M_CAP_PCT` (`:26`) is covered by **no version string** — `RULES_VERSION = "v1"` (`:24`) and `RULES_V2 = "v2"` (`store_ld8.py:36`) **label the two stored series, not the rules and not the cap** (both series run the same maths — see the same-maths note above) — so changing that constant is a **contract event (@both)**, not a refactor: every stored row's M cuts would lose their stated derivation and no column records it.

**Scope is a run-level dimension, and an unattended fire fans it out per brand.**

`--brand-id`/`--group-id` (`scripts/run_pipeline.py:338-339` → `JobCtx`, `pipelines/schedule.py:227`) reach **every** job of a batch via `_scope_args(ctx)` (`pipelines/schedule.py:410`), so one invocation scopes the *whole* pipeline, not one job — which is why per-brand rows require **one runner invocation per brand**.
Every job joins that argv through `_with_common` (`pipelines/schedule.py:440`), whose `with_scope` defaults true, so a manifest is what opts a job out.
A fire with no pinned scope takes its batches from the live `brands` table (`tick_scopes`, `pipelines/schedule.py:97`; enumeration `TICK_BRANDS_SQL`): every brand, then the `(NULL, NULL)` hq roll-up last (`scripts/run_pipeline.py:254-256`) — and the manifest's `scopes:` block names the same, no group (`057_manifest_scopes_brands_hq.sql`).
A run may not stamp a group scope: `pipelines/scope_shape.py` `require_selectable_scope` refuses `(None, group_id)` and `(brand_id, group_id)` at the CLI boundary — before a flag is converted and before the connection — so `scripts/run_pipeline.py`, `scripts/run_daily_snapshot.py` and `scripts/run_upload.py` never reach the cluster with a scope no reader can select.
The pair is the shape a group cut would need, and the guard is relaxed in the same change that gives the portal a session scope which can promise it — the rule's canonical home is the manager root (`../docs/conventions.md` § Scope shape).
A hand-run that pins a scope stays exactly that one batch (`pinned`, `:251`).
Data engines stamp the run context directly (`_run_data_engine` `pipelines/schedule.py:766`; `KPI_DAILY_UPSERT_SQL` `pipelines/snapshot.py:82`, `COHORT_SNAPSHOT_UPSERT_SQL` `pipelines/snapshot.py:176`), so the fan-out covers them with no argv change, while the ml writers receive their `--brand-id` from the same `_scope_args` (`pipelines/schedule.py:410`).
The stored crons are `nightly` (`0 1 * * *`: apply, rfm, churn, churn_all, clv, **segments_purchase, segments_overall, segments_behavior**) and `weekly` (`0 2 * * 1`: segments_purchase, segments_overall, recs, assoc, explain, forecast, cohort, matrix).
The three segment modes moved to nightly in `041` because `segments_behavior` was in no manifest at all (a page fed by a job nobody ran) and the other two made an explain-vs-segment mismatch take up to seven days to heal.
`weekly` keeps its own segments copy because `assoc`/`explain`/`matrix` declare `depends_on: segments_overall`, so folding them out would leave dangling names, and the duplicate run on the weekly night is a `skipped` row on the day grain.
`kpi_daily` is in neither manifest **by decision** (the manager verified no app reader and no `/analytics/kpi` route: a nightly write into a table nobody serves is tick time for no user) — the manifest line goes in with the first KPI surface.
`forecast` moved to nightly in `045` (~1.8 s per brand × six brands ≈ 11 s, measured; a weekly cadence let the demo brand's batch age seven days while `churn`/`clv`/`rfm` refreshed nightly).
The job carries `046`'s pinned `lookback_weeks: 106` — a re-stated manifest that copies 041's `52` silently reverts the window widening, which `services/api/tests/test_manifests.py` now catches by comparing the two crons.
A brand-scoped run stamps `--brand-id` only — a `(brand, group)` stamp is invisible to the demo session, which resolves `brand_id = X AND group_id IS NULL`.
Measured on the demo dataset: one nightly fire is **7 batches** (6 brands — `DEMO`/`SUNHING`/`KVLN`/`VICEW`/`HARBOR`/`PEAKLENS` — plus hq) × 4 jobs = 28 job runs in **34.5 s**, with each writer stamping its own row per brand (`rfm_snapshots` four window rows per scope) and 0 failures, so a weekly fire pays the same fan-out over 8 jobs.
Before the fan-out a brand-scoped session got live RFM at `n=435` while `GET /analytics/rfm/snapshots` returned **0 rows** and `movement` a **404 `need_two_snapshots`** — **never a platform limit: nothing wrote scoped rows** (the tick now does) and no UI action calls the POST, while a scoped POST works (`services/api/tests/test_rfm.py:380` asserts `201`).
Find-or-create is scope-aware (`ml_engine/store_ld8.py:65-66`), so the fan-out adds its rows beside the hq row instead of replacing it, and the single demo group makes a `--group-id` run customer-identical to hq.
No ML change is needed (`ml_engine/store_rfm.py:29-31`; `run_ld8` already threads `brand_id`/`group_id`), so per-brand writes are a **runner** decision.
The fan-out does not fix the scoped role that night: the band pairs **two** `current` rows of the newest `rules_version` in scope, so each scope needs a second scoped night.
The accurate copy is scope-neutral (*"no movement for this scope yet — movement appears once two snapshots of this window exist for this scope"*), because "the view needs a second night" is false of a scope whose own row count never moves.

**Group scope is conjunctive by design, and its only silent exclusion is pre-group customers.**

A group run requires `customers.group_id = g` **and** `brands.group_id = g` (`db/migrations/011_groups.sql:6` — customers shared within a group with NO linkage across clients, a hard silo).
`brands.group_id` is NOT NULL (`:23`); `customers.group_id` is NULLABLE on purpose (`:86`, the same legacy pattern as `consents.brand_id` / `NULL = pre-brand`, `:95-99`), so a group-scoped run **excludes** those rows silently.
On the live demo that half is a no-op (1 group `DEMO`, 6 brands, and **no customer with a NULL `group_id`**, read 2026-10-21), so it is a contract property, not a testable one, and it has **no coverage in either suite** (app's group tests at `services/api/tests/test_groups.py:90`, `:133`, `:155`, `:217`, `:287`, `:331` never put a NULL-`group_id` customer under a group run).
The switch that would **include** those rows instead of dropping them is the `IS NOT DISTINCT FROM` form — **declined on purpose, not missed**: 011's design is exclusion, and a group run means the group's pool, not "the pool plus unassigned rows".

**`IS NOT DISTINCT FROM` locates a stored artifact by its recorded scope; the `IS NULL OR` guard scopes a live population.**

The two forms must not be confused, and they fail in opposite directions.
The population form's NULL test is on the *parameter* (`$3::uuid IS NULL OR …` = "no restriction on this column"), so a **stamped** surface using it would **disclose** `NULL/NULL` rows into a brand session — a visibility bug.
The artifact form compares *column to parameter* NULL-safely (`IS NOT DISTINCT FROM …` = "only rows whose stamp is NULL here") and can only **hide**, which is this system's quarantine default.
The rule is **two-step**: locate the row whose visibility the predicate decides — a **stamped** row takes stamp-equality on its scope stamp, a live or joined source row takes the population form even when the column in hand is called `brand_id` — then ask what the column *is on that row*: a scope-stamp column of the located row takes `IS NOT DISTINCT FROM` (a NULL there means "computed globally" and must fail closed), a non-scope attribute of that row or a dimension of a joined row takes `IS NULL OR` (a NULL there means "no restriction").
- One statement, one row, both forms: `SNAPSHOTS_LIST_SQL` `store_rfm.py:57 WHERE ($1::text IS NULL OR mode = $1)` is the population form for `mode` (NULL = every mode), `:58-59` the artifact form for the scope stamp (`:56` is only `FROM rfm_snapshots`).
- One file, the same two scope columns, opposite operators: `store_churn.py:193-198` (`SNAPSHOT_FIND_SQL`, artifact, guarding `:196-197`) vs `:253-261` (`CUSTOMER_TX_ORDERS_SQL` / `CUSTOMER_TX_ACTIVITY_SQL`, population, guarding `:258-259`, `:263-281`).
- File-level: `analytics_snapshots.sql:22-23` (artifact, stamped row) vs `:110-111` (population, joined `b.id`/`g.id` dimensions) — which kills a per-file label.
- All **11** `IS NOT DISTINCT FROM` predicate pairs in `ml_engine/` are the artifact form (`store_churn.py:193-198`, `:301-302`, `:311-312`, `:323-324`, `store_clv.py:168-169`, `store_forecast.py:31-32`, `store_ld8.py:65-66`, `store_recs.py:88-89`, `:103-104`, `store_rfm.py:58-59`, `store_segments.py:388-389`); the guard's *reads* are the population form (`store_churn.py:253-261`).
- The aggregate filter is population form — `AND ($4::uuid IS NULL OR c.group_id = $4::uuid)` (`ml_engine/store_rfm.py:30`; the guard form is three lines `:29-31`, and the last two guard the **same** `$4`, so a customer whose own group differs from their store brand's group is dropped by the brand line) — whereas the *listing* query uses `brand_id IS NOT DISTINCT FROM $3::uuid` / `group_id IS NOT DISTINCT FROM $4::uuid` (`store_rfm.py:58-59`).
- Both shorthands are wrong alone: the column *name* is not the rule (`brand_id` sits under both operators at `store_rfm.py:29` vs `:58`), and the row's *kind* is not the rule (one row can carry both forms).
- App's reference decision is `../crm-cdp-app/services/api/analytics/scope.py:1-51`: a stamped row is visible iff `row.brand == session.brand AND row.group == session.group`, NULL-safe on both sides, with **quarantine-by-default** — `NULL/NULL` legacy and global rows are **HQ-only**, so an unstamped write is not an error but a row only HQ can see (`row_visible`, `scope.py:62-67`).
- Because the predicate is **mutual**, a brand-less HQ session `(None, None)` matches only `NULL/NULL` rows, so brand- and group-stamped rows are invisible to HQ **by construction** (`row_visible`) — deliberate, not a defect.
- Where the intent really is "this session's rows *or* the global ones", the population form cannot express it; app writes the disjunction out explicitly as an **added term**, never a relaxed operator (`scope.py:27-30`).
- `session_scope` returns a brand scope `(brand, None)`, the global scope `(None, None)`, or **no scope at all** — a staff row that carries a group and no brand has nothing to read and is refused out loud with 403 `no_analytics_scope` (`session_scope`, `no_scope_error`), and the ROLE is not part of the rule any more: a manager linked to a brand reads that brand, exactly like an `sa` linked to the same brand (app `15be3e0`, read at `df20bd7`).
- The group is a customer attribute, not a scope: no session carries one, so a group-stamped `(NULL, G)` row is read by nobody — an under-read, never a leak (`stamp_scope` names the row's own stamp) — and the PAIR `(brand, group)` is the shape a group cut would need (see the scope-shape rule above).
- App's population-form hits are all source-row filters over `orders`/`customers`/`brands`/`groups` (`analytics_snapshots.sql:110-111`, `analytics_simulate.sql:16-18`, `analytics_recs_fallback.sql`, `stats.py:23-29` `_ORDER_SCOPE`/`_CUST_SCOPE`) — none is a stamped surface, so no leak.
- "admin can't see the brand runs" is a **visibility-policy** question for the contract and the owner, never a WHERE-clause tweak in a migration.

**Third writer, non-scheduled:** `POST /analytics/rfm/snapshots` writes **v1** through the same `store_rfm.create_snapshot`, stamped `created_by=user.staff_code` (`analytics_rfm.py:132-142`).
It is an ordinary v1 append: it changes the v1 row inventory, not the pairing rule above, and with the cap pinned it cannot store a fit the v1 engine path could not express.

## Batch upload ETL (frontend files land here)

App posts the file edge (`upload_jobs` row); data owns stage → validate → quarantine → commit.
`014_upload_staging.sql` + `pipelines/upload.py` + `scripts/run_upload.py`.
The flow per file is idempotent on `file_hash` x kind x scope — re-upload = same job, no double-commit.

1. **receive:** SHA-256 hash + `upload_jobs` row (`received`).
2. **stage:** raw CSV lines → `staging_rows` (`pending`).
   - CSV only in v1; gzip accepted (magic-sniffed, 10MB raw / 64MB decompressed caps).
   - orders `lines` column carries a JSON list (cents may be decimal units).
   - A line may carry the optional opaque `rec_ref` token - see "053" below.
3. **validate:** per-kind `pipelines/ingest` normalize (`customers` / `orders` / `events` / `identity`) → `ok` / `rejected`.
4. **apply (`--apply`, `pipelines/apply.py`):** staged `ok` rows into core tables, idempotently, one transaction per job.
   - Customers upsert on `member_id` (+ contacts append, conflict-ignore).
   - Orders resolve store (id-or-code) + customer (uuid/member/external) + SKUs, pre-checking `(source, external_order_id)` so replay = skip.
   - The insert itself is `ON CONFLICT DO NOTHING` on that same partial UNIQUE, so a row that lands between the pre-check and the insert (a concurrent apply) is skipped and counted instead of failing the whole job.
   - Events pre-check `(source, source_event_id)` with the same conflict-ignore on insert.
   - Identity binds the external id onto the existing member row.
   - Unresolvable refs (unknown store/customer/SKU/member) → apply-fail quarantine, so `total == committed + quarantined` still holds (committed = applied + dedup-skipped).
   - Optional raw `ordered_at` (ISO) honoured for orders, else now().
5. **finish:** rejects → `quarantine_rows` (source, key, reason, raw, `code`) + job counts + `upload_jobs.quarantine_reasons` = `{code: count}`.
   - Invariant `total == committed + quarantined` (`reconcile()` raises on drift).
   - Without `--apply` the job ends `validated` (staged-ok counted committed, core untouched); with it, `committed` / `committed_with_rejects` / `failed`.

014 also backfills the idempotency columns the contracts always assumed: `orders(source, external_order_id)` + partial UNIQUE, and `customer_events.source_event_id` + partial UNIQUE `(source, source_event_id)`.
Core-table apply is SHIPPED (`pipelines/apply.py` + `--apply`); the app-side thin `POST /uploads` edge is the remaining piece.

## 015: segments + scheduled pipelines (ml part 2, data-owned tables)

`015_segments_pipelines.sql` declares four tables.

- `segment_runs` (as_of/mode purchase|overall/k/seed/centroids + features jsonb/inertia/rules_version/scope, COALESCE-scope UNIQUE).
- `segment_assignments` ((run, customer) PK, segment/label; customer_id only per 013).
- `pipeline_runs` (pipeline/job/as_of/queued|running|succeeded|failed|skipped/run_id/upstream/error/scope, UNIQUE).
- `pipeline_manifests` (one validated YAML row per pipeline; edits via app config API, never migrations).

Centroids are TRANSFORMED-space (purchase L2-taste / overall zscore).
The space tag rides in `model_training_runs.params` (ml writes the lineage row, model='segment').
Inertia is run-quality only, never a gate.
Seeds: nightly + weekly content-verbatim from the agreed example YAML (split per pipeline, ON CONFLICT DO NOTHING so admin edits win).

**Segments k.**

- The seed pins `k: 5` EXPLICITLY on every `engine: segments` job, because serving reads the LATEST run for (mode, k, scope) and its own default is `k=5` (app segment/explain routers, `k: Query(ge=2, le=20) = 5`).
- An unwritten k is not a soft miss: the read answers 404 `no_snapshot` with `available_k`.
- A cadence that only ever fits k=6 therefore leaves the product's default surface dark right after a wipe while every job reports succeeded.
- The product exposes exactly ONE k (no picker; `apps/web/app/segments/page.tsx:561-564` — the file's own comment says "No k picker", so the claim and the cite agree), so one k is enough and a miss is answered with guidance built from `available_k`.
- Extending to a ladder (e.g. {5,6}) is a manifest edit if a second k ever gets a consumer, not a code change.
- Do NOT "serve the latest available k" instead -- that relabels k=6 clusters as the k the caller asked for.
- Pairing runs across days: key a per-segment series on `(as_of, k)` and read `k > count(DISTINCT label)` as an EMPTY segment, never as a missing row.
- Measured on the live cluster: every brand scope is `k=5` on every day the seed pins; the global scope still carries a `2026-10-16` `k=6` run beside its `k=5` days; `2026-10-20` `behavior` has `k=5` with 4 labels (`overall` on `2026-10-16`: `k=6`, 5 labels).
- k is a CALLER PARAMETER with a configured value, never a model choice: no engine selects k (no elbow/silhouette anywhere in ml -- the fit computes `inertia` for whatever k it is handed, and `store_segments` records `k_requested`/`k_used`).
- The page copy's "fitted by the model (elbow/silhouette)" states intent (staff do not choose k), not mechanism; do not "resolve" that wording by adding auto-k selection, which would fight the pin.
- All four places name the same value: the 015 seed, the runner default, ml's CLI default (`66fe0c7`), and the live `pipeline_manifests` rows.

**Runner.**

- `pipelines/schedule.py` + `scripts/run_pipeline.py --pipeline <name> --ml-repo <path>` (cron entrypoint).
- Tick-time DB resolution: `--db-url` > `$DATABASE_URL` > live pgembed pidfiles re-read every tick (app checkout, then cwd) — ports are random per restart, never pinned.
- No live postmaster = loud failure, never a fresh empty boot.
- Manifest parsed from the table (`--manifest-file` dev override never writes).
- Topo order over depends_on (cycle = error).
- Data engines in-process (upload_apply = sweep validated jobs, kpi_daily = KPI upsert, cohort_snapshot = retention upserts per granularity — `DATA_ENGINES`; params `granularities` + `max_offsets`, defaults week/month/year 12/12/5).
- ml engines as `python -m ml_jobs.run_* DB_URL ...` subprocesses (thread-based: safe on SelectorEventLoop) with DATABASE_URL passthrough; stdout run-id parsed, else NULL.
- Re-runnable: succeeded skipped unless `--force`; upstream failure → downstream `skipped`.
- Cron owns the tick; the runner fans a brand-scoped job out into one batch per brand (`--once`/`--watch` `tick_scopes`; a one-shot `--pipeline` run takes one `--brand-id`).
- The nightly document's last job is `future_rows_guard` (`future_rows`), which fails the tick naming `table: count` for any `as_of`/`scored_at` dated after the tick's own day.
- The guard waits for EVERY other nightly job (`depends_on` = the nightly job set minus the guard itself, `058`): a writer it does not wait for can leave a future-dated row AFTER the guard has passed, and the guard stays green. The list is derived from the job set, not written by hand, so a new job breaks the check instead of rotting it.
- Prevention lives in the two writers (`guard_as_of` refuses `as_of > business_today()` unless `JobCtx.allow_future`, ml's CLI refuses before connecting), detection lives in the tick.
- The detection is needed because a future-dated row outranks the live number on every `as_of DESC` reader and a `pipeline_runs` `succeeded` claim for a day that has not happened makes the real tick skip the job.

**Scheduler ownership is a split, not a to-do for ml.**

- `pipeline_manifests.schedule` is a *declaration* whose 5-field shape ml validates (`ml_jobs/manifest.py:141-142` `is_valid_cron` — "Light 5-field cron shape check. The runner owns full parsing.", called only at `:189-190`/`:218-219`) and never evaluates.
- ml has no scheduler entry point at all (every entry is a one-shot `python -m ml_jobs.run_*` that connects, writes, exits — no loop, no daemon).
- So wiring a trigger is a change to **this repo's runner only, zero ML change**.
- The **feature** half is still open (015 gives manifests + `pipeline_runs`, but nothing in the product schedules a tick or shows one — app, after 015).
- The **infrastructure** half shipped 2026-10-17 as `[ADDITIVE]`.

### The in-repo tick: `--watch` / `--once` `[ADDITIVE]`

`scripts/run_pipeline.py --once` (one pass — the unattended entry point) and `--watch [--interval 60]` (that pass in a loop).
Each pass reads `pipeline_manifests` and fires every row whose `schedule` matches the business clock (`pipelines/schedule.py` `cron_matches` — 5-field Light cron; Sunday `0` or `7`; `dom`/`dow` **ORed** when both are restricted, Vixie semantics).
A field it cannot read raises `ManifestError`, logged per row, never skipped silently.
`--only a,b` filters rows; `--now` pins the clock (dev/verification; naive = `Asia/Hong_Kong`).

- **The stored cron is a declaration and the tick is one evaluator of it, never the authority.** Any trigger calling `--once` at least once a minute is equivalent (Task Scheduler, cron, systemd timer), and a competing trigger is safe — the runner skips, below.
- A host cron that bypasses the table (the `--pipeline <name>` entrypoint) is still a **hand-run**, not a tick: its rows carry no `upstream.tick`.
- **Attribution.** Each fire gets one id (`uuid4().hex[:12]`) stamped on every row it writes as `upstream.tick`. 015 `pipeline_runs` has no actor column, so this is what the table can hold: tick-written rows are distinguishable from hand-runs, rows within one fire are not. The stamp is opt-in on `JobCtx.tick` (default `None`), so `--pipeline` rows are byte-identical to what they were before this change.
- **Idempotence is the runner's, not the loop's.** `pipeline_runs` is unique per (pipeline, job, `as_of`, scope) and a `succeeded` row skips unless `--force`, so a second tick for a window already succeeded writes nothing new — overlapping triggers, a restart and two hosts converge.
- A row that is `running` refuses the claim (`UPSERT_RUN_SQL` returns no row), so two cursors in one cron minute cannot execute the same job twice: the loser skips that job *and* its dependants (outcome carries `blocking`).
- A `running` row older than `RUN_STALE_AFTER` (2 h) is reclaimed, so a killed tick does not wedge the job forever.
- The loop's `fired` set only stops one process from re-entering the same window inside one `as_of`; a `failed` row is retried once per process start, not every interval (a failure is a row carrying `error`, not something to hammer).
- **Every pass is one JSON line on stdout** (`event`: `pass`, `fired`, `fire failed`, `bad manifest`, `row name differs from its yaml`, `no database`, `once: done`): the log is the loop's only record, and a loop that logs only its fires is indistinguishable from one that died.
- SIGINT stops the loop, logs it, exits `130`, and leaves an in-flight job's row `running` with no `finished_at` — the next tick retries it (skip needs `succeeded`), so an interrupted window self-heals and that row is the evidence.
- **Not built:** no daemon or supervisor, no catch-up for windows the process missed while down (a missed `as_of` is a gap, not a backfill — re-run it explicitly), and one namespace per invocation (scopes still fan out as one run per `--brand-id`/`--group-id`).

Do not read a green `pipeline_runs` row as proof a scheduler exists; that holds for rows without `upstream.tick`, which are hand-runs.

**Run provenance** (ADDITIVE, no migration).

- A job that ran as an ml subprocess carries `ml_repo_sha` (full 40-char HEAD of the `--ml-repo` checkout) and `ml_repo_dirty` (bool) inside `pipeline_runs.upstream`, resolved once per pipeline run (`git -C <repo> rev-parse HEAD` plus `status --porcelain`).
- The resolution is best-effort — git absent or the path is not a checkout gives `null` for both and the tick continues, so an unlabelled row means "could not resolve", never "old row".
- Data-engine rows keep `{"engine": ...}` alone **on purpose**: a revision on a row whose numbers came from in-process data code is a mislabel.
- This labels the *tree the engines were executed from*, which is a different question from ml's per-file `as of <sha>` pins (last commit touching the cited file, e.g. `ml_engine/store_ld8.py:70-72` naming `ac39625` while the checkout's HEAD is ahead of it).
- The checkout is not a submodule and no revision is pinned anywhere, so `ml_repo_dirty = true` means that run has **no** revision at all — the sha is a starting point for `git diff`, not a reproducible input.
- app's `/api/v1/pipelines/runs` already returns `upstream` untouched (`db/queries/pipelines.sql` `runs_list`, parsed by `_run_to_dict` (`services/api/routers/pipelines.py:105-122`)), so a consumer needs no API change.
- The sha is the checkout's HEAD **at run time**, never the last commit to touch some file (`git log -1 --format=%h` over a path answers a different question and is not a substitute for it).
- It is written on the **failure** path too: the label is resolved before the engine is invoked and both finish paths persist it (`pipelines/schedule.py:828`, `:852-863`, with `error` carrying the other half), because a failing run is exactly when the revision is worth knowing.
- `test_runner_labels_a_failed_job_too` (`services/api/tests/test_schedule.py`) pins that alongside the resolved, dirty and unresolvable cases.

## 016: modeling-upgrade flag mapping (ml registry / algorithms / signals)

`016_segment_behavior_mode.sql` widens `segment_runs.mode` to purchase|overall|behavior (additive).
Runner maps the new ml argv.

- churn/clv `--model` (default bgnbd, pass-through — ml validates against the registry).
- segments `--mode` + `--algorithm` (kmeans|gmm|agglomerative|dbscan|hdbscan, default kmeans).
- recs `--content` (default off) + `--include-user-cf` when true.

Seeds untouched (example YAML still validates).
App mirrors note: app carries verbatim 013/015 CREATEs for isolated test bootstrap — re-mirror 016 on update; live dev DB needs 016 applied (through 015 already).

## 019: source connector framework (D1, [BREAKING ingest])

`019_connectors.sql` + `pipelines/ingest/connectors.py`.
Source types are equals — `shopify | csv | excel | delta_lake | rest_api`.
`woocommerce` is RESERVED: registration rejected until its spec lands; widening the CHECK is the additive path.
Every source funnels into the same normalize/load contracts above; per-type work is field mapping only.

| source_type | required config | field mapping |
|---|---|---|
| `shopify` | `shop_domain` (+ optional `api_version`) | order `id` → `external_order_id`; `line_items` → `lines` (sku/qty/price→cents); shop/location → `store_id` resolve |
| `csv` | `column_map` (+ `delimiter`, `encoding`) | header/column map → contract fields (member/order/event/identity kinds) |
| `excel` | `column_map` (+ `sheet`, `header_row`) | same as csv after sheet/header selection |
| `delta_lake` | `table` (+ catalog/path) | table scan → column map → contract fields; checkpoint in `connector_sync_runs` |
| `rest_api` | `base_url` (+ pagination/auth refs) | paginated JSON per entity → contract fields |

**Validate-on-create** (`validate_connector`).

- Unknown type rejected.
- Reserved `woocommerce` rejected.
- Per-type required keys enforced.
- Any credential-shaped config key (`*_secret`, `*_token`, `*_key`, `*password*`, …) rejected — secrets are never embedded in config.

**Sanitize-on-read.**

- `sanitize_for_log` redacts PII + secrets for logs.
- Quarantine `raw` keeps PII (exact-match reprocessing) with secrets redacted at write (`redact_secrets`).

**Secrets-at-rest DECISION.**

- `connectors.config` = non-secret params only.
- Credentials live in env/secret-manager, `secret_ref` names the env var (e.g. `SHOPIFY_TOKEN`).
- The DB never stores a plaintext secret.
- Rotation = update env, no DDL.

**Sync-run history DECISION.**

- New `connector_sync_runs` (connector + entity + status + rows_seen/rows_ok/rows_rejected + error + scope).
- It is NOT an extension of 015 `pipeline_runs` (different grain: sync run vs scheduled job; different readers: connector health vs cron orchestration).
- `pipeline_runs` stays job-grain, untouched.

**Write/trigger API [ADDITIVE]** (`services/api/routers/connectors.py`).

- All **six** mutations carry `Depends(require_admin)` (`Admin` at `:60`); the three GETs are not gated.
- The rule is written as a rule, not a count: "Every mutation below is admin-gated" (app `327a48b`), so it cannot drift again.
- The data-access module `services/api/repos/connectors.py` sits **below** the auth layer, where a gate would be the wrong place and is absent by design.
- Secrets + execution outrank uploads.
- `POST /connectors` (201; 409 duplicate (source_type, name); 422 validation).
- `DELETE /connectors/{id}` (204, runs cascade).
- `POST /{id}/sync {entity, scope?}` (202 queued; 422 on disabled = kill-switch, no force).
- `POST /{id}/test` (stored-or-prospective check, no persistence: config-shape + secret-presence).
- `PUT /{id}/secret {value}` (performs the 019 rotation: sets the env var at runtime, touches `updated_at`, no DDL; 422 without `secret_ref`; process-scoped, lost on restart — secret-manager remains the pilot path).

**LD2 per-connector schedule + sync checkpoint** (`025_connector_schedule.sql`, [ADDITIVE]).

- `schedule_cron` NULL = manual-only, else validated 5-field cron (`validate_schedule_cron`; range-checked minute/hour/dom/month/dow).
- `PUT /connectors/{id}/schedule` set/clear (404 `connector_not_found`, 422 bad cron).
- `last_sync_at`/`last_sync_status` (`queued|running|succeeded|failed`) are stamped by the **sync worker**, not by the serving layer.
- `pipelines/ingest/connector_sync.py` `note_sync_result(run, status, counts)` closes the run row AND the connector checkpoint in one transaction (raises on bad status/counts or missing run).
- The API only reads the checkpoint — `GET /connectors/{id}` renders it, `PUT /{id}/schedule` touches `schedule_cron` alone.
- App made the checkpoint **worker-owned** in its connector pass, which is why the writer is a pipeline module rather than a `services/api/repos` one: data's `4fa0cc3` had landed it inside the API copy of an app-owned file, and app's later edit dropped it there.
- The API still enqueues the run (`POST /{id}/sync` → `queued`); everything after that is the worker's.

**Incremental-extract helpers** (no I/O).

- Optional `pagination` config block (`page|cursor|offset`), cursor REQUIRES `cursor_param` — page-only extracts rejected as a smell; validated at registration; `default_limit` 1..1000.
- `since_pushdown` (server-side `since` first for `shopify|rest_api`, client-side for file scans).
- `since_filter` with fail-open dating (undateable rows INCLUDED + flagged for quarantine — never silently dropped).
- `should_retry` (429/5xx/transport only, never 4xx) + `compute_backoff` (base·2ⁿ, capped).
- Per-run structured `sync_log` lines.
- `describe_extract` shape probe for connection checks.

**Not owned:** the scheduler tick and the actual extract/load worker — this is vocabulary + checkpoints so the app-side scheduler has something honest to drive.
LM1/LM2 PLANNED (joint with ml, additive migrations AHEAD of their engines): `btyd_params` (per-scope fitted BTYD params) + `assoc_rules_eval` (per-batch holdout metrics) — data to land the tables first, ml builds engines on our grain after.
Every response carries `has_secret` (presence bit) only; values are never stored, logged, or echoed.
CONVERGED (app ruling 2026-10-15): `has_secret` means env-live everywhere (can this connector authenticate right now?) — reads compute it Python-side from the stored `secret_ref` name, same as writes; 404 code aligned on `connector_not_found`.
No migration (019 covers the tables).
VENDORED COPY (2026-10-15): app mirrors `pipelines/ingest/connectors.py` (+ `__init__` chain) into its tree for serving; the data copy stays canonical — registry/validation changes here need an explicit re-mirror, app never edits its copy.

## 020: cohort retention snapshots (D2, [ADDITIVE] analytics)

`020_cohort_snapshots.sql` + `COHORT_SNAPSHOT_UPSERT_SQL` / `COHORT_TREND_SQL` / `retention_rate` in `pipelines/snapshot.py`.
Grain (agreed with app): one row per (`granularity` x `cohort_period` x `period_offset` x scope) with `cohort_size` + `retained` + `retention_rate`.
App serves `GET /analytics/cohort?granularity=week|month|year` + scope filter from this ONE table (retention line + heatmap).
Math: cohort of a customer = `date_trunc(granularity,` first completed order).
`retained(k)` = cohort members with ≥1 completed order in period (`cohort_period` + k·granularity).
`retention_rate` = `retained`/`cohort_size` (NULL when empty; offset 0 is 1.0 by construction).
Week periods start Monday.
Writer re-run RE-MEASURES its cells: `ON CONFLICT (granularity, cohort_period, period_offset, COALESCE(brand_id, '00000000-0000-0000-0000-000000000000'::uuid), COALESCE(group_id, '00000000-0000-0000-0000-000000000000'::uuid)) DO UPDATE`, so a cell whose window closed after the first write stops reading 0.
The conflict target must carry the grain index's EXPRESSIONS (`cohort_snapshots_grain_ux`): bare columns cannot infer an expression index, and a wrong target fails at runtime, not at review.
The re-run re-measures the WHOLE cell - `cohort_size` included: a cohort's size DOES move (a customer whose first completed order lands in an earlier period joins that cohort), and the reader's denominator is the sum of the per-cohort sizes, which must equal the scope's distinct customers (the disjointness assertion below); a frozen size with a live numerator would push `retention_rate` above 1.
It re-measures `retained`, `retention_rate` and `retained_cents` with it.
A re-run also DELETES the cells the population no longer produces - this granularity and this scope only - so the grid is exactly the run's output.
Every row the run writes takes the run's write time, so one grid carries ONE vintage.
The cohorts are DISJOINT (a customer sits in exactly one cohort per grain), so the per-cohort sizes add up to the scope's distinct customers; the writer asserts it (`assert_cohorts_disjoint`), because the reader's headline IS that sum and an overlapping cohort would overcount it with no signal.
`056_cohort_population_rule.sql` adds `population_rule` to the same table: the rule that places a customer in a cohort, in words the reader can print; the writer's `POPULATION_RULE` is the column's DDL default, byte for byte.
No ML needed (cohort is a data-owned agg).
Weekly cadence rides the scheduler (`cohort_snapshot` engine, same job family as the forecast snapshot).
Scheduler also carries argv builders for `assoc_batch` / `explain_batch` / `forecast_snapshot` / `matrix_batch` (ml CLI contracts verbatim).
`explain_batch` and `matrix_batch` manifests store `segment_run_id` (concrete UUID, `latest`, a missing key, or the all-zero UUID — every form resolved at dispatch to the newest segment run for `segment_mode` +/- `segment_k` + scope, so manifests never hold a stale fixed id).
`052_cohort_money.sql` adds `retained_cents` to the same table - the money cohort members brought back in each period, summed from that period's completed orders.
ARPU is `retained_cents` / `retained`; the column is `NOT NULL DEFAULT 0`, so a row written before the migration reads 0 cents, which is "not measured" and not "no money".

### The reader's contract (app, committed)

App owns the read: `GET /analytics/cohort` in `../crm-cdp-app/services/api/routers/analytics_cohort.py`, over app's own `../crm-cdp-app/db/queries/analytics_cohort.sql`; these are the same facts, read at app master `af4a179`.
A scoped session reads `cohort_cells` / `cohort_periods` / `cohort_granularities` - `brand_id IS NOT DISTINCT FROM`, `group_id IS NOT DISTINCT FROM`, `period_offset <= max_offset`.
An HQ session (no brand, no group) reads `cohort_cells_global` / `cohort_granularities_global` - the global rows only, never every brand's grid merged into one.
The reader pins the latest row per grain in SQL (`DISTINCT ON (cohort_period, period_offset)` + `created_at DESC`), so it never renders an older vintage's cell; the vintage it NAMES is the business day (+08) of the newest write time it read, and a grid whose cells carry more than one write time is reported as mixed.
The denominator is the SUM of the per-cohort MAX of `cohort_size` (the size is repeated on the cohort's offset rows); a cohort whose rows disagree about their size is reported, not averaged away.
The row carries the population it describes (`population_rule`, data 056); when the rows disagree about the rule, the reader reports no rule at all.
`retained_arpu_cents` is `retained_cents` / `retained` in integer cents, and NULL when `retained_cents = 0` (0 cents means "not measured", not "no money").
No rows in scope is 404 `{"code": "no_snapshot"}`.

## 053: recommendation attribution (D3, [BREAKING ingest])

- **The token.** `rec_clicks.rec_ref` is a uuid minted by the server that serves the recommendation - one row per served recommendation, opaque to the merchant.
- The mint is DETERMINISTIC (uuid v5 over `batch_id`, `customer_id`, `product_id`, `rank`) and it lives in ONE module, `pipelines/rec_ref.py` (`mint_rec_ref`, `NAMESPACE_REC_REF` derived as uuid5 of the RFC 4122 DNS namespace and the name `crm-cdp/rec_ref`).
- The serving side mirrors that file byte-for-byte, because a second implementation is a second answer to "what is this token" and the two answers would meet on the same order line.
- Change the namespace and every token in flight stops resolving, which is the same failure as deleting the click rows.
- **The write is an UPSERT, and a repeat is normal.** "The staff sent this twice" must not raise: `INSERT ... ON CONFLICT (rec_ref) DO UPDATE SET served_count = rec_clicks.served_count + 1, last_served_at = now()`.
- `served_at` is the FIRST serve and never moves, `last_served_at` is the most recent one, `served_count` is how many.
- `rec_clicks` is the one mutable table in this pair (it counts serves); `order_recommendations` is APPEND-ONLY - one row per order line, never updated, because a fact that was true once does not change.
- **The payload.** An order line grows ONE optional field, `rec_ref`, in the `lines` list (`pipelines/ingest/orders.py` `normalize`); its absence is NORMAL, and no token means no attribution row.
- **The scope of a read comes from the CUSTOMER, never from the batch.** `rec_clicks.batch_id` is nullable and ages with its batch, so a scoped attribution read that filters on the batch's brand would silently under-count from the first retention sweep - and an under-count that looks like a floor is not a floor.
- Join the customer (`customers.group_id`; the brand follows the customer's stores). The reader's rule is also written into the `053` migration header.
- **The writer.** Data writes the attribution at apply time (`pipelines/apply.py` `_apply_order`), per ORDER LINE (`order_recommendations`): the token is resolved against `rec_clicks` and the click's product and customer are compared with the line's product and the order's customer.
- **The mismatch rule.** A token that names no served recommendation, or one whose product or customer is not the line's, is a disagreement: it quarantines the row with both values named, the same discipline the feed-total rule already uses.
- Never attribute a recommendation to a different product, and never drop the line quietly.
- A token with no `rec_clicks` row is UNKNOWN (nobody ever sent it), not unattributed - only the serve row can say which.
- **The absence rule.** No token, no attribution row, and a reader must say "no attributed revenue yet", never $0 - a $0 page and a "no shop has sent attribution yet" page are different facts, and only the second is true.
- **The floor.** An attributed revenue number is a LOWER BOUND, never the batch's revenue: the orders that carried no token are invisible to it.
- The reader says "at least $X" (app's copy rule) and the claim vocabulary says `not_measured` for "nothing served yet, or nothing came back".

## 021: SHAP cluster-explainer result tables (M2, [ADDITIVE] analytics)

`021_explain.sql` declares two tables.

- `explain_batches` (segment_run_id FK → `segment_runs` cascade + mode/k/seed/top_n/counts/scope).
- `explain_rows` (top-N per batch+cluster by mean|SHAP|: rank/feature/mean_abs_shap/mean_shap/min_shap/max_shap, UNIQUE against double-write).

PNGs stay MLflow-only; app serves latest-run summaries.
Shape mirrors `ml_engine/store_explain.py` inserts.

## 040: SHAP batch coverage (`clusters_explained`, `clusters_skipped`)

`store_explain.explain_matrix` already returned how many clusters it explained and which cluster NUMBERS it skipped (too few members / all one cluster).
`explain_batches` had nowhere to put them, so a batch that dropped clusters looked identical to a full one and the app could not render "segment 3 was not explained".
`040` adds `clusters_explained integer` and `clusters_skipped integer[]`, both NULLABLE with no default: a batch written before 040 reads `NULL` = **not recorded** (the `seg_surrogate_fits.accuracy` convention), never a false "explained 0, skipped none".
ml binds both in `BATCH_INSERT_SQL`; app renders `NULL` as not recorded and a non-empty `clusters_skipped` as the per-segment skip.

## 022: demand-forecast snapshot tables (M3, [ADDITIVE] analytics)

`022_forecast.sql` declares two tables.

- `forecast_batches` (as_of/method/horizon_days/lookback_weeks/category_count/skipped_count/scope, grain UNIQUE for idempotent weekly cadence).
- `forecast_rows` (one row per batch x category x Monday-week `period_start`: predicted_qty + 80% interval lower_qty/upper_qty, CHECK-ordered, UNIQUE against double-write).

Method `trend_seasonal_naive`; thin categories counted in `skipped_count`, never silent.
Shape mirrors `ml_engine/store_forecast.py` inserts.
M4 simulator needs no migration (pure in-process engine, no DB).

## 026: report definitions registry (LD11, [ADDITIVE] analytics)

`026_report_definitions.sql` + `pipelines/reports.py`.
Report pages are app-served, but their DEFINITIONS live here so snapshots and pages can't drift.
`report_definitions(report_id, version, name, kind, grain, definition jsonb)` pins (axes, metrics, filters) per report version.
Immutable rows (`REVOKE UPDATE/DELETE`, same convention as 013): a new version is a new row (`ON CONFLICT (report_id, version) DO NOTHING`), readers take MAX(version) via `LATEST_DEFINITIONS_SQL`.
`validate_report_definition` requires `report_id/name/kind/grain` + `version >= 1` + at least one of `axes|metrics|filters` in `definition`.
Any RFM/design writeup rides inside `definition` under `rfm_note` — deliberately no second table.

## 027: landing receipts (LD12, [ADDITIVE] ingest)

`027_landing_receipts.sql` + `pipelines/receipts.py`.
One receipt per landed batch: file uploads (`upload_job_id` FK, UNIQUE — one receipt per job, `ON DELETE RESTRICT` so landed proof outlives nothing) OR vendor batch imports (`vendor_batch_id` external key) — CHECK exactly one of the two.
Counts reconcile (`loaded + quarantined <= seen` in both validation and DDL).
`contract` names the contracts.md section the batch validated against.
`manifest_pipeline` links `pipeline_manifests(pipeline)` — NULL for manual/ad-hoc loads, FK enforced otherwise.
Immutable (`REVOKE UPDATE/DELETE`, same convention as 013/026).
`validate_receipt` is the writer-side gate; `RECEIPTS_FOR_JOB_SQL` serves the per-job proof.

## 028: fitted BTYD params (LM1, [ADDITIVE] analytics, joint with ml)

`028_btyd_params.sql` + `validate_btyd_params` / `LATEST_BTYD_PARAMS_SQL` in `pipelines/model_fits.py`.
Data lands the table FIRST; ml's fitting engine writes rows matching this grain.
One row per (as_of x activity x horizon_days x scope x model): `params` `{r, alpha, a, b}` all > 0, `n_customers`, `loglikelihood`, or `fit_error` when the fit fails (CHECK: params or error, never neither).
MODEL STRINGS (034, post-repoint fallout 2026-10-15): canonical values are UNDERSCORE `bg_nbd`/`pareto_nbd`.
028 shipped hyphen `bg-nbd` but every production-shaped write bounced on the CHECK while ml's writer+readers were already underscore-consistent, so data moved (CHECK swap, no backfill — no live hyphen rows could exist).
`validate_btyd_params` rejects hyphen.
Grain mirrors `churn_snapshots` (013) NULL-safe (COALESCE sentinel UNIQUE) so the serving read joins scope-to-scope; refit = new `as_of` row, never UPDATE (REVOKE, same convention as 013).
`LATEST_BTYD_PARAMS_SQL` serves the newest successful fit per grain (failed fits stored but invisible to serving) — this is the read behind alive_path / prob_n / expected-purchases.
ml: conform your fit writer to these columns; request changes while still additive.

## 029: cluster-vs-population matrix (LM2, [ADDITIVE] analytics, joint with ml)

`029_cluster_matrix.sql` + `validate_matrix_row` / `MATRIX_FOR_RUN_SQL` in `pipelines/model_fits.py`.
CORRECTION (2026-10-15, pre-push, never applied live): this number first held an assoc-holdout-eval table from a misread of the LM2 item — the agreed LM2 was always the comparison matrix (legacy-steal plan + ml triage).
The number was replaced, not supplemented.
One row per (segment_run x cluster x feature x layout): cluster mean/median vs population mean/median + diff + pct_diff (NULL when population median is 0).
`layout` 'wide' (matrix cells) vs 'long' (UNPIVOTED UI rows).
Runs die → matrix dies (CASCADE, same as 021 explain rows).
Immutable (REVOKE UPDATE/DELETE).
ml: write per-feature rows per cluster per run in either layout; serving reads one layout per run.

## 032: long-layout matrix (LM2, [ADDITIVE] analytics, joint with ml)

`032_matrix_long.sql` + `validate_long_row` / `LONG_MATRIX_UPSERT_SQL` / `LONG_MATRIX_FOR_RUN_SQL` in `pipelines/model_fits.py`.
Answers ml's grain question: wide's PK (run, cluster, feature, layout) cannot hold melted stat rows, and stat-name-in-feature would pollute the parsed feature namespace — so long gets its own table, no PK surgery on 029.
One row per (run x cluster x feature x stat); stat is one of the six wide value columns by name (`cluster_mean`, `cluster_median`, `population_mean`, `population_median`, `diff`, `pct_diff`).
`value` NULL-able (pct_diff NULL on zero population, same rule).
Runs die → long rows die (CASCADE).
Immutable (REVOKE UPDATE/DELETE).
ml: `long` layout no longer raises — write one row per stat per feature per cluster.

### `matrix_batch` engine (runner, [ADDITIVE], joint with ml)

- `pipelines/schedule.py` registers engine `matrix_batch` alongside `explain_batch` (`ENGINE_BUILDERS`); both dispatch through the shared `_resolve_segment_run_id`.
- A manifest's `segment_run_id` (concrete / `latest` / missing / all-zero) becomes the newest `segment_runs` row for (`segment_mode`, optional `segment_k`, scope) at dispatch.
- No matching run = job `failed` with an actionable ManifestError and downstream jobs `skipped`.
- Containment is the whole dispatch, not just the resolver: the resolve call and `ENGINE_BUILDERS[engine](...)` both sit inside the job's `try` (`pipelines/schedule.py:912-914`: the resolve call is `_resolve_segment_run_id`).
- So a malformed manifest (a wrong `params` key raising in `_build_matrix`, an `argv` builder refusing its input) also lands as `status = 'failed'` with `type(exc).__name__: exc` on that job's `pipeline_runs` row (`pipelines/schedule.py:897`) and the tick carries on with the remaining jobs.
- Consequence for debugging: a wrong key is **discoverable, not dark** — the reason is on the run row and in the tick's `outcomes[<job>]['error']`; it just has no serving-time symptom, so no API response will ever point at it.
- Query `pipeline_runs.error` (or the tick result) before assuming a job never ran.
- CLI (ml, `ml_jobs/run_matrix_batch.py`): `python ml_jobs/run_matrix_batch.py DB_URL --segment-run-id UUID`.
- Argparse declares nothing else, so the runner passes exactly DB_URL + `--segment-run-id` — **no `--as-of`, no `--brand-id`/`--group-id`, no `--created-by`**; a flag the CLI does not declare is an exit-2 on every tick.
- Read the "no `--created-by`" separately from the scope clause — scope and `created-by` are independent, and `explain_batch` differs on the second only.

**What a job's argv may carry is per-engine, and the split is what a *row* can carry.** On a scoped tick (`services/api/tests/test_schedule.py:495-504` pins all three):

| engine | receives `--brand-id`/`--group-id`? | receives `--created-by`? |
|---|---|---|
| `explain_batch` | **no** — scope rides the resolved segment run (`pipelines/schedule.py:588-589`, `_with_common(..., with_as_of=False, with_scope=False)`) | yes |
| `matrix_batch` | **no** — same reason | **no** (argparse declares none) |
| `assoc_batch` | **yes** — reads the catalog directly and no row carries a scope for it (`pipelines/schedule.py:568`, `with_scope` default) | yes |

- Rule, and the reason a blanket sentence about it would be wrong: scope arrives as argv only for a job that reads the catalog directly; a job that reads a resolved **row** (`segment_runs`) takes its scope from the row.
- An argv copy would be a second source of truth that can disagree with the run it actually read (ml's review catch, fixed `bce2721`).
- Exit codes are a signal (taxonomy confirmed by ml): 0 = success (both layouts written); 2 = argparse refusal, which only a drifted argv builder can produce; 1 = a genuine runtime failure -- unreachable DB, a `segment_run_id` the writer rejects, or any exception inside `store_matrix` (a non-UUID id lands there too, `uuid.UUID()` raising before any DB work).
- The resolver only ever hands over a concrete UUID, so rc=1 with a resolver-produced id is precisely the resolver/writer disagreement worth pinging ml about, while rc=2 from the tick is a contract drift on the data side.
- One invocation writes BOTH layouts (029 wide + 032 long).
- Weekly placement: immediately after the segments run it reads, `depends_on: [segments_overall]` (mirrors `ml_jobs/pipelines.example.yaml`).
- Manifest rows stay edited through the app config API, never migrations (015 rule), so the live weekly row gains this job there; manual invocation needs no manifest change.
- Guard (honest refusal, not a silent empty table): the job rejects non-centroid backends (`dbscan`/`hdbscan`) with an unprocessable error, same as the explain path — if `segments_overall` were ever configured with dbscan the matrix stays dark by design and the tick records the refusal on its `pipeline_runs` row.

## 030: surrogate-fit registry + explain method labels (LM1, [ADDITIVE] analytics, joint with ml)

`030_surrogate_fits.sql` + `validate_surrogate_fit` in `pipelines/model_fits.py`.
Per the ACKed LM1 method spec: one fit row per segment run (`seg_surrogate_fits`: segment_run_id UNIQUE FK CASCADE + `trained_at` + `accuracy` 0..1 + `feature_names` + `xgb_params`).
Accuracy lives on the fit row ONLY.
`explain_batches` gains `fit_id` (NULL for mean-method rows, SET NULL if the fit row goes).
Batches + rows gain `method` (`segment_mean` (all history — the default, zero backfill rewrite) | `xgb_surrogate_v1` (gate passed) | `surrogate_unfitted` (gate failed, NO shap rows)).
`validate_explain_write` (merge `844b770`, NOT the label-only `validate_explain_method`) was the LM1 writer-side gate: unfitted + shap_rows>0 raised; v1 required a fit_id AND accuracy >= 0.90.
**Both labels are historic only as of ml `9072b73`:** that repo's `EXPLAIN_METHODS` is now `("segment_mean",)`, so `explain_run` and `explain_matrix` refuse `xgb_surrogate_v1` and `surrogate_unfitted`, and every new batch carries `fidelity_measured = FALSE` with `accuracy` NULL.
The gate itself lives at `ml_engine/store_explain.py:359` (ml's check), NOT here — data's copy of the labels had no caller and still admitted a method ml had deleted, so it was pruned rather than kept in step.
A reader keys on the row's own `method`: rows written before `9072b73` keep `xgb_surrogate_v1` with a real `fit_id`/`accuracy`.
No tick writes under `xgb_surrogate_v1` until spec ACK — ACKED with this migration; ml may write.

## 031: time-aware RFM storage (LD8, [ADDITIVE] analytics, joint with ml)

`031_rfm_time_aware.sql` + validators/SQL in `pipelines/snapshot.py`.
Per the ACKed LD8 joint contract: per-window snapshots reuse `rfm_snapshots`/`rfm_customer_scores` + a `window_key` label (`current` rolling-365/LD5-as-of | `mom` prev calendar month | `yoy` same month prior year | `lifetime`).
`rules_version` 'v2' on time-aware rows (column pre-existed, default 'v1' — v1 history untouched).
New: `rfm_segment_sizes` (as_of x window_key x segment: n + median_r/f/m; diff/pct vs comparator served by query) and `rfm_segment_movement` (as_of x pair x from x to: `current_x_mom`|`current_x_yoy`, n + pct_of_from).
Both pairs are written by the **same run** from its own window assignments (`ml_engine/segment_movement.py:22` `PAIRS`, `:23-26` `PAIR_WINDOWS` = `("mom", "current")` / `("yoy", "current")`, `store_ld8.py:255-263`), each row stamped with that run's `as_of`.
One night populates movement, and a second *date* is needed only for a second `current` (and `lifetime`) **snapshot** — what the app's same-window diff pairs — never for the cross-window pairs.
There is no `lifetime` pair; `lifetime` is a window, not a comparison.
ENTERED (only as from) / EXITED (only as to) sentinels for newcomers/exited — uppercase, never collide with lowercase segments; pct_of_from NULL enforced (outside the flow pct denominator by construction).
Lesson: `window` is a PG reserved word — column is `window_key`.
ml executes the movement math verbatim via `validate_movement_row` / `validate_segment_size` + the UPSERT/SELECT SQL.
**Scope:** both aggregates are **unscoped outputs** — no scope column, no parent FK, global by construction (see **Unscoped outputs** below).

## 037: backtest outcomes (M2, [ADDITIVE] analytics, joint with ml)

`037_backtest_outcomes.sql` (+ `038` grant re-statement, `039` read indexes).
One row per customer per model per holdout window, metrics `auc`/`spearman`/`calibration_ratio` beside the realized outcome they were scored against.
Column names are the shipped ones — `freq`/`holdout_days`/`predicted`; ml's prototype `frequency_model`/`horizon_days`/`predicted_value` lost because the migration landed first and `ml_engine/backtest.py:117-123` was fixed to match (`858bd98`, and a test now pins the list).
**Append-only, in two parts:** the unique key is the *window identity* (`COALESCE(brand_id, group_id)` + `model, anchor_as_of, holdout_days, customer_id`), which stops a duplicate row.
Immutability is the `REVOKE UPDATE, DELETE … FROM PUBLIC` (027/029 shape), and a cluster that recorded 037 before that line existed gets it from `038` (`035` reasoning: the runner skips a recorded version (`scripts/bootstrap_db.py`), `REVOKE` is idempotent; regression guard `services/api/tests/test_migration_drift.py`).
**ml writes this table and never reads it** (`grep -rn backtest_outcomes ml_engine/ ml_jobs/` matches only the INSERT), so `039`'s read index has no consumer yet.
The per-window read it is measured against (0.575 → 0.268 ms live, 2026-10-19) belongs to app or a later report — do not wait on ml for a read-side number, and the half-table window aggregate keeps its seq scan by measurement (0.50 vs 0.45 ms).
**Scope:** carrier (below).

## The stamped set: which relations carry a scope stamp (app+data+ml, 2026-10-17)

A **stamped relation** carries a scope stamp (`brand_id`/`group_id`) on the row itself or inherits exactly one through its parent artifact: the set `row_visible` decides on (`../crm-cdp-app/services/api/analytics/scope.py:1-51`, reference `:72-76`), and the set the artifact-vs-population rule above applies to.
**Derive the group from the schema, never from the name.**
Over `db/migrations/*.sql` `migrate:up` bodies only: **23 = 16 carriers + 7 negatives**; never union the `migrate:down` direction (it drops `012_analytics_scope.sql:26-32`, `031_rfm_time_aware.sql`).
**Visibility (`row_visible`).** Visible ⟺ NULL-safe stamp-equality between the row's own stamp and the reader's scope (`row.brand == session.brand AND row.group == session.group`; session = brand `(brand, None)` or global `(None, None)` — a group scope does not exist any more, so no session reads a group-stamped row).
The equality is **mutual**: HQ matches only NULL/NULL rows (legacy pre-scope rows, admin-created global batches), and no session reads a group-stamped row (the group scope was withdrawn; a staff row that carries a group and no brand has no scope at all).
Never relax it to the population form to "let admin see brand runs" — a brand session would then match global/legacy rows; for "own rows or global" write the disjunction.

### Carriers — 16
The row's **own** stamp decides visibility: `rfm_snapshots`, `rec_batches`, `clv_snapshots`, `churn_snapshots`, `kpi_daily_snapshots`, `cohort_snapshots`, `segment_runs`, `assoc_batches`, `explain_batches`, `forecast_batches`, `model_training_runs`, `pipeline_runs`, `connector_sync_runs`, `upload_jobs`, `btyd_params`, `backtest_outcomes` (037; `ml_engine/backtest.py` writes it, nothing reads it yet — a future reader owes it the stamp predicate).

### Inheritors — 14
**No scope column of their own**: visibility follows the parent's stamp through an FK, so a scope predicate on an inheritor can only be evaluated by **joining the parent**. The parent→child edges are the whole list:
- `rfm_customer_scores` ← `rfm_snapshots` (`snapshot_id`); `churn_customer_scores` ← `churn_snapshots`; `clv_customer_scores` ← `clv_snapshots` (all `snapshot_id`)
- `segment_assignments` ← `segment_runs` (`run_id`); `cluster_feature_matrix`, `cluster_feature_matrix_long`, `seg_surrogate_fits` ← `segment_runs` (`segment_run_id`) — the two `cluster_feature_matrix*` relations are **one logical matrix** (`031` `layout IN ('wide','long')`; `032` melts the grain the wide PK cannot hold), so a guard covering one misses the other
- `assoc_rules` ← `assoc_batches`; `explain_rows` ← `explain_batches`; `forecast_rows` ← `forecast_batches`; `rec_items` ← `rec_batches` (all `batch_id`)
- `landing_receipts`, `quarantine_rows`, `staging_rows` ← `upload_jobs` (`upload_job_id`)

**A group is a property of the edge, not of the child.** Five children carry a second, non-scope parent edge (`rec_items`, `segment_assignments`, `rfm_customer_scores`, `churn_customer_scores`, `clv_customer_scores` reference `customers` via `customer_id`), and `landing_receipts` a second optional parent (`manifest_pipeline → pipeline_manifests`, `027_landing_receipts.sql:27-28`); key a guard on the **edge**, not the table.
Thirteen of the fourteen edges are `NOT NULL … ON DELETE CASCADE`; the exception is a boundary case: `landing_receipts.upload_job_id` is `uuid NULL UNIQUE … REFERENCES upload_jobs (id) ON DELETE RESTRICT` (`027_landing_receipts.sql:16-17`), because exactly one of it and `vendor_batch_id` must be set (`:31-33` `CHECK ((upload_job_id IS NOT NULL)::int + (vendor_batch_id IS NOT NULL)::int = 1)`), so it inherits on the **file-upload** path only: a **vendor batch import** receipt has no parent artifact, no scope column and no join to a stamped relation (unscoped by construction), and a guard must skip on the **edge**, not the table — with it empty that `CHECK` alone holds the invariant, so an empty read proves nothing.

### Negatives — 7
Live entities carrying a scope column that is **not** a visibility stamp: `brands` (`group_id`), `customers` (`group_id`), `stores` (`brand_id`), `staff` (`brand_id`), `membership_tiers` (`brand_id`), `campaigns` (`brand_id`), `consents` (`brand_id`). A predicate over these is an ordinary filter over live rows.

### Unscoped outputs — 3
Two shapes, split by what the relation stores:
1. **Holds scope-dependent results with no scope column** — readers must gate, writers must abstain: `rfm_segment_sizes`, `rfm_segment_movement` (031; writer ml; the writer choice is **B′**, below). No predicate can distinguish a per-scope row from a global one.
2. **Holds no results at all** — no obligation either way: `report_runs` (app-only, `../crm-cdp-app/db/migrations/033_report_runs.sql`; data carrying no 033 is the bootstrap-subset rule, not drift) is a scope-free **ledger** with no result column. Grouping it with shape 1 invites the one wrong fix available here: scope columns on a table that carries no computed content.

**Their writer choice: B′, decided 2026-10-18.** Nothing links the two 031 aggregates to a scope: the writers `SEGMENT_SIZE_UPSERT_SQL` and `MOVEMENT_UPSERT_SQL` (`pipelines/snapshot.py`) are `ON CONFLICT … DO NOTHING`, the readers `SIZES_FOR_WINDOW_SQL`/`MOVEMENT_FOR_PAIR_SQL` filter `as_of` + window/pair only, so their rows are **global by construction** (first-writer-wins).
A scoped LD8 run still writes its scope-stamped snapshots/scores and **skips** both scope-free 031 aggregates (`rfm_segment_sizes`, `rfm_segment_movement`) — `run_ld8` is the writer half (`ml_engine/store_ld8.py`: `write_sizes`/`write_movement` sit behind `if not scoped:`) — and the skip is **reported as skipped**, never a bare zero: the job returns `sizes: null`/`movement: null` plus `skipped: [{table, reason}]` built from `SCOPED_SKIP_TABLES`, since "nothing changed" and "not written for this scope" are indistinguishable to a reader.
The rejected alternative **A** (a scoped run writes them anyway) would **make** a brand's aggregates the global rows for every unscoped read, and `defer` leaves that hole open.
The reader half stays (`../crm-cdp-app/services/api/routers/analytics_rfm_windows.py:1-24`: scoped sessions 403): it closes only reading, which is exactly why the writer half carries the fix.

### Obligations per group, and what a guard can assert
- **Carriers (16) and inheritors (14)** share one *reader* invariant: a query over an inheritor is safe only when whoever resolved its parent did so **under the scope predicate**. That is a property of code, not data, so a DB test over these rows asserts **nothing** — there is no second column to compare.
- **The enforcement is not a sweep over the SQL text**: measured (app, 2026-10-17), such a sweep flags 23 of 23 query blocks and every flag is correct code, because the **caller** resolves the parent in scope and passes its id down, so the child query has no parent join by design (`../crm-cdp-app/services/api/routers/analytics_explain.py:54-64` + `../crm-cdp-app/db/queries/analytics_explain.sql:19-25`). The checkable invariant is cross-file: for every child-query call carrying a parent id, the same code path must first scope-guard that parent. That guard is **not built**.
- **Carriers with a second artifact parent — 2 edges, not the same case.** `explain_batches → segment_runs` (**writer: ml**): equality is structural (the child's stamp is copied from the parent row read in the same call) — live 2026-10-17 **7 compared / 0 violations**, the paired run-identity check (`explain_batches.fit_id → seg_surrogate_fits`) **2 / 0**.
- `pipeline_runs → connector_sync_runs` (**writer: data, both sides**) is **not** one statement: the sync row takes the session's scope (`services/api/repos/connectors.py:218-224`), the run row's stamps come as caller flags (`scripts/run_pipeline.py:113-116`) stored by `pipelines/schedule.py:75-89`, and **the runner never reads the sync row's stamps**. Live **0 rows** — untested, not verified; the harness is data's, because the writer is.
- **Carriers that also inherit — the row's own stamp governs.** `explain_batches.segment_run_id uuid NOT NULL REFERENCES segment_runs (id)` (`021_explain.sql:18`) sits alongside its own `brand_id`/`group_id` (`:29-30`); `pipeline_runs.sync_run_id` (`023_pipeline_sync_link.sql:12-13`, NULL) is the same shape. **The row's own stamp governs** (app ruling 2026-10-17): it is the only column `row_visible` reads, and the parent FK is lineage, not a second visibility path. A child/parent stamp disagreement is **a write bug**, not a case to define away: guard it at the writer that inserts both in one transaction (`ml_engine/store_explain.py explain_run`).
- **Negatives (7)** have no visibility obligation (an ordinary population filter); unscoped shape 1 gates at the reader and abstains at the writer, shape 2 has neither. **Expressible is not enforceable**: seeds with NULL scope at both ends cannot fail a stamp-equality assertion — teeth need writer-driving tests (ml's `store_explain` / `store_ld8` / `store_rfm`, data's `test_schedule.py::test_sync_lineage_stamps_agree`).

**The trap in any schema-derived edge list.** Enumerating FKs over scope-carrying tables yields **25 edges with a stamp at both ends, 23 of them into dimension relations** (`brand_id → brands.id` ×20, three into `customers`/`stores`). Exclude them: a brand-scoped row carries `group_id = NULL` while its `brands` row carries the owning group, so equality there is **wrong by design**. Only the **both-stamped** artifact→artifact edges (the 2 above) count.

**A third artifact→artifact edge, whose invariant is not a stamp.** `explain_batches.fit_id → seg_surrogate_fits.id` (`030_surrogate_fits.sql:29-30`, `uuid NULL … ON DELETE SET NULL`) is not between two stamped ends — `seg_surrogate_fits` (`:15-24`) carries `segment_run_id` and no scope column — so assert **run identity** instead (`explain_batches.segment_run_id IS DISTINCT FROM seg_surrogate_fits.segment_run_id` must be empty), not a stamp.

## The group filters and their populations (2026-10-22)

**The form follows the meaning.** A **population** filter compares a group with `=`; a row's **own** scope column is compared with `IS NOT DISTINCT FROM` — a bare `=` there drops the global row (a NULL scope).
**The families are four.** `conjunctive` — the customer's group AND the brand's group, read with the SAME scope parameter (the KPI and cohort writers). `customer-only` — the customer's group alone (a customer carries no brand). `brand-row` — the brand's group alone, about a brand's own rows. `scope-exact` — a row's own scope column.
**A customer population honours the brand side** (ml's rule, `549ba79`): it narrows by the brand's group or by naming the brand; a fact about a brand's rows narrows by the brand's group.
**No statement compares `group_id` against two parameters** (ml's rule): one scope parameter per statement, so a reader cannot be shown two scopes' rows in one result.
**The check is `services/api/tests/test_scope_predicate_shape.py`** — it scans every SQL literal in `pipelines/**` and every statement in `db/queries/*.sql` and pins the legal form, the family each statement's predicates describe, and that **no group filter is unclassified**; its teeth are `scripts/mutation_check_scope_predicate.py`. The pin freezes and reports the families; it does not unify them — a family is a property of the statement, not a defect to edit away.

## Tree ownership & the mirror boundary (manager ruling 2026-10-22)


- **The rule is the manager's** (`../AGENTS.md` § Ownership, `../docs/conventions.md` § The shared statement) and is not restated here; data's half is that it owns `db/migrations/**`, `db/queries/**`, `db/seeds/**`, and app owns `services/api/**` (data mirrors it).
- **A shared file carries the SERVING form** (manager ruling, 2026-10-22): a `db/queries/<resource>.sql` statement is what the runtime serves, and a runtime that inlines a copy of a named statement is token-equal to it. The ETL's unscoped need lives in the repo that owns the pipeline.
- **The fork class is the file whose runtime INLINES a copy** — `db/queries/orders.sql`, `customers.sql`, `products.sql`, `identity.sql`; the parsed class (`consents.sql`, `customers_search.sql`, `pipelines.sql`, `analytics_snapshots.sql`, `kpi_daily.sql`) is read by its `-- name:` marker, so it cannot fork.
- **Every named statement of the four inlined files is served verbatim** (33 of 35): the customer, order, product, inventory, staff and user reads were folded to the runtime's own text on 2026-10-22; the exceptions are `page_clv_scores` and `page_churn_scores`, data's own read path, which have no runtime copy.
- **The guard is `services/api/tests/test_shared_statement_serving_form.py`** — 33 named pairs (every named statement of the four files, paired with its runtime counterpart BY NAME; a counterpart that is not found fails the check rather than skipping it) plus a check that every named statement is served verbatim; its teeth are `scripts/mutation_check_shared_statement.py` (15 mutations, both directions).
- **The teeth cannot lie about the tree** (2026-10-22): the script refuses a dirty tree, takes a lock so two runs cannot interleave, and proves at the end that the touched files match the commit — an earlier version restored only to its own snapshot, so a residue survived every later run while the receipt said `restored: True`.
- **Data holds a subset, not a mirror** (the data-only `test_apply.py`, `test_upload.py`, `test_schedule.py`, … are harness, not drift); `services/api/tests/**` is data's own, so a sync copies the runtime and data updates **its** tests when app's runtime changes shape.
- **The KPI rollup's read is shared** (2026-10-22): `db/queries/kpi_daily.sql` carries the four serving reads of the daily rollup (`kpi_trend`, `kpi_pct_change`, `clv_snapshots_list`, `churn_snapshots_list`); app mirrors the file byte for byte and serves the dashboard from it.
- **A module holds no copy of a shared statement** (2026-10-22): `pipelines/snapshot.py::KPI_TREND_SQL` is read from the file (`_shared_statement`), so the module and the file cannot drift.
- **Closure — the unit of copying is the import closure, not the module.** Mirror a module with the files it reads *at import* (a module-level read), or the tree imports and cannot boot; a spec-file read stays opt-in via an explicit argument, never a module-level constant.
- **A moved writer's layer is not a fork:** `note_sync_result` (data's logic in data's copy of the app-owned `services/api/repos/connectors.py`) now lives in the data-owned `pipelines/ingest/connector_sync.py`.
- **The mirror has a check now** (`services/api/tests/test_mirror_runtime.py`): every shared runtime file under `services/api/` — minus `services/api/tests/**`, data's own harness — must byte-equal app's HEAD blob, normalised for the line endings; an absent sibling skips the check unless `MIRROR_REQUIRED=1`. A mirrored file with no mirror test is a copy, and a copy drifts.
- **No cross-tree file reads at runtime**: each tree boots from its own copies, and what crosses the boundary is declared — app's `pip install -e ../crm-cdp-ml` and vendored `pipelines/ingest/connectors.py`; data executing ml's checkout as a subprocess on every ml job; ml's SQL depending on the schema these migrations create.

## `pipeline_runs`: what a reader may rely on (2026-10-20)

**Stored statuses are three, not five.** The tick writes `running` when it claims a job (`UPSERT_RUN_SQL`), then `succeeded` or `failed` when the job ends; `queued` and `skipped` are in the `status` CHECK (`015:61`) but the tick never writes them — a job that did not run leaves **no row at all** (upstream failed, the claim gate refused it, or the tick died before claiming it).
A reader must compare the rows against the manifest's job list, not count rows: a missing `(job, as_of, scope)` is the signal that it never started, and a `running` row is the only one that needs a staleness test.
**Every exit writes a terminal row.** `run_pipeline` catches `Exception` per job and `BaseException` (SIGINT / `SystemExit` / `CancelledError`) with a re-raise, so a job that is interrupted ends `failed` with `error = "<ExceptionClass>: <message>"` (≤2000 chars, `_finish_failed`) instead of staying `running` — the row a human needs to find a death without reading a log.
**`running` older than `RUN_STALE_AFTER` (`interval '2 hours'`) is dead, not busy** — one literal feeds both the re-run gate and `STALE_RUNNING_SQL`/`stale_running()`, so a health view cannot call a 3-hour row healthy while the tick calls it abandoned.
**`upstream` names the engine and, for an ml job, the revision it ran at** (`{engine, tick?, ml_repo_sha?, ml_repo_dirty?}`, built before the try, so a *failed* row still carries the sha; absent means "could not resolve", never "old row").

## The expectation floor: what "no row" means per job (2026-10-20)

A `pipeline_runs` row exists only when the tick fired the job, so a reader that shows an empty cell cannot tell "never ran" from "not yet due" without knowing what the schedule expects.
That expectation is **not** a constant in `pipelines/schedule.py`: it is the manifest row, so a cadence edit needs no code change.
Shape of `pipeline_manifests.yaml` (`{version, pipelines: {<name>: {schedule, scopes, jobs: {<job>: {engine, depends_on?, params?}}}}}` — `jobs` is a mapping, not a list).
A job's cadence is the cron of the **most frequent** manifest that names it, because a job may appear in several: `forecast` is in both `nightly` (`0 1 * * *` — the default grain is `daily`, so "daily") and `weekly` (`0 2 * * 1`), and the frequent one is what a reader should claim.
Nightly jobs: `apply`, `kpi`, `rfm`, `churn`, `clv`, `segments_purchase`, `segments_overall`, `segments_behavior`, `forecast`, `future_rows`. The guard's `depends_on` is that list minus the guard itself (see the scheduler section).
Weekly only: `recs`, `assoc`, `explain`, `cohort`, `matrix`.
`kpi` (engine `kpi_daily`) joined nightly in `051`: the writer and the engine already existed, but no manifest named it, so `kpi_daily_snapshots` held a single 2026-09-30 row and the table was dead on both ends (nobody scheduled it, nothing served it). The job runs right after `apply`, so KPI history accumulates from the tick rather than from the day a page appears. Its absence in a row-less cell is therefore a missed run like any other.
App already serves this without a new data surface: `GET /api/v1/pipelines/manifests` (`services/api/routers/pipelines.py`, pipeline + `yaml` + pipeline-level cron) and `GET /api/v1/pipelines/manifests/{pipeline}` (parsed jobs + dependency edges).
Combine those with `pipeline_runs` and a page can say "`forecast` runs daily, last row 3 days ago" and "`cohort` runs weekly, last row 3 days ago — not yet due" instead of rendering the same empty cell for both.

## The customer list's value and risk: one snapshot, one row per customer (2026-10-21)

The customer list has to answer "who is my most valuable customer" and "who is about to leave" out of the scores the nightly writes, and a bare number cannot carry that answer: it needs the **vintage** it came from and the **scope** it was measured in.
Two statements do that - `page_clv_scores` and `page_churn_scores` in `db/queries/customers.sql` (app mirrors the file token for token, so the two trees cannot drift).
**One snapshot, not one per day.** `clv_snapshots` and `churn_snapshots` hold one row per customer per day, so the read picks the NEWEST snapshot in scope and then looks each customer up through the `(snapshot_id, customer_id)` primary key of `clv_customer_scores` / `churn_customer_scores` - the page's row count does not depend on how much history the tables hold.
**The vintage always.** Every value travels with its `as_of` and its snapshot id, and the churn band also travels with the `(activity, horizon_days)` pair it was measured on - a band without its pair and its date is a number with no meaning.
**"Not scored" is NULL, never zero.** A customer the snapshot does not carry reads NULL for every score column while the vintage still travels, so a reader can tell "this scope has no snapshot" (a NULL `as_of`) from "this customer is not in that snapshot" (an `as_of` with no value beside it).
**The scope of the score is the scope of the list.** The session's `(brand_id, group_id)` is compared with the snapshot's own stamp by equality, the rule the other snapshot reads already use (`db/queries/kpi_daily.sql` `clv_snapshots_list`): a brand session reads its brand's snapshot and no other's — neither the global one nor a group's, and no session reads a group-stamped snapshot at all since the group scope was withdrawn.
**A snapshot dated after today is not a fact yet**, so both arms bound the snapshot by the +08 business date the snapshot writers use (`pipelines/snapshot.py` `business_today()`).
The cost is a plan, not only a shape: the snapshot is chosen once and the planner answers each customer with one index probe through the `(snapshot_id, customer_id)` primary key, so the price of the join does not grow with the table's history.
Which pairs exist is ml's to answer, not the reader's: `store_churn.available_pairs` (in `ml_engine/store_churn.py`) is the set of `(activity, horizon_days)` a scope can be served, with the rows present and the count the writer stamped.

## The churn `all` leg is a nightly job, not a tab (2026-10-21)

The portal's churn page offers an "All activity" tab, and no `activity = 'all'` snapshot had ever been written: the tab 404s and the empty state says "run the pipeline first", which is false on a system whose pipeline has run.
The fix is one manifest row (`055_manifest_churn_all_nightly.sql`): no ml change, no DDL, no new column - `churn_snapshots` already carries `activity`, and the argv comes from the job's params (`pipelines/schedule.py` `_build_churn`).
**What the leg promises, and what it must not.** ml measured the cohort at brand scope: 0 customers in `all` but not in `orders` (the same 498 either way), with 2 of 498 re-timed (expected purchases 214.7 -> 216.7, +0.9%), so the copy may say "the same cohort, read on more activity" and must not say "a different population".
**The flags are ml's, and they are pinned here:** `--activity all --horizon-days 90 --model bgnbd` (ml master 0326402), no `--rescore` (the `all` leg is its own leg of the unique key `(as_of, activity, horizon_days, scope)` and writes its own row), and horizon 90 to match the nightly `orders` leg - 180 is not a promise for `all` (the only 180 pair that exists is an HQ one from 2026-10-15).
**`created_by` stays the tick's stamp**, like every other nightly leg; the leg is identified by `activity`, which is the column a reader filters on.
The `future_rows` guard now depends on the leg, because the guard reads `churn_snapshots` and the leg writes into it.
`services/api/tests/test_churn_all_manifest.py` holds all of this against the STORED manifest and the built argv, so a later re-statement that forgets the leg turns red there instead of leaving a dead control in the product.
