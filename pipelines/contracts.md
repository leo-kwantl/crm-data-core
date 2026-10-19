# Source → table contracts

Reading citations here: the **symbol** is the anchor (`store_ld8.run_ld8`, `_checked_body`,
`PAIR_WINDOWS`), never a number — a line number is stated only with an `as of <sha>`, since a bare
line number breaks silently at the next commit. Cite what you read, at the revision you read it
(`git show <sha>:<path>`), and end the citation where the *statement* ends, not where your reading
did. A **constant offset** between two readings means two revisions, not an error: `git show` both
before publishing a correction. A pin is **per cited file, not repo HEAD** (`git log -1 --format=%h --
<path>`) and is **regenerated at paste time, never copied**, since a copied value rots before the doc
stores it.

Citations also name a **tree**: `db/**` and `services/api/**` resolve to **app's** checkout,
`pipelines/**` to **data's** (see the mirror-boundary section at the end of this file). A path
mirrored byte-identically in two trees cannot carry a pin — **currency is the tree's property,
content is the revision's** — and a bare basename is a coin flip. Resolve by tree, and name the tree
when the number matters.

**Two genres, and the difference is what a sha can be checked against.** A path token (bare or
backticked; backticks are punctuation, not the rule) immediately followed by a backticked sha **on the
same line** is a **pin** — equality with that file's last-changed revision. A sha carrying a cue word
(`read at` / `measured at` / `as of`) is a **label** — existence plus ancestry, and with a path, that
the path was there at that sha. A label proves **never** content: a count-neutral edit leaves every
line valid while replacing the words at them, so only a human re-verifies a quotation. A default run
is **markdown only** and `scripts/audit_doc_cites.py` prints that boundary beside its counts; a sha
with neither a path nor a repo name is unjudgeable, reported as `unclassified`, never guessed. A row
id is not a revision, and one pin never covers several paths or a wrapped pair.

A count, by contrast, is a claim about the invocation that produced it, and the guard prints its
harness — coverage mode, peer set, tree — beside every bucket. Publish the line, not the number: one
unchanged tree reports a different `ok` in markdown-only and `--comments` mode, and `--peer`
**replaces** the defaults, so declaring a single peer turns every other tree's cites into findings
*about the docs* rather than about the invocation. The same trap once covered the peer set, which
appeared nowhere in the output, so the invocation had to travel with the line for `cite-blank` and
`cite-tree` to be re-derivable at all; every printed line now names its peers (`d61978b`, `ef94a56`),
and that half of the rule is **retired by the line itself** rather than demanded of authors -- prefer
the mechanism over the discipline wherever the mechanism can be built. A count from a superseded
invocation is
**withdrawn, never diffed** against a fresh one, and the withdrawal is a property of the pair: name
the set you are superseding in the same message, restating that one hop and marking anything older as
superseded rather than restating it -- the earlier message cannot be edited, and a chain of
live-looking sets is the very failure this rule exists to prevent.

Every zero is one of three different claims, and they are not interchangeable: a **mechanical green**
(the tool decided it), a **peer-verified green** (someone read the span at a named revision — the only
form available where there is no symbol to name), and a **checked skip** (the bucket declined
knowably: `cite-symbol-unchecked`, `rowid-skipped`). A report that shows only "0 findings" flattens
exactly the third, which is the one that carries the information for a span with no symbol in it.

A grep-unique statement name is the primary cite; a bare `:NN` is legitimate only where the file is
named in the same sentence, and a bare number with **no referent in scope** clears the checks
vacuously. **"Your file still says X" is itself a claim that needs its revision**: quote it with
`git show <sha>:<path>`, and read `git log --oneline -- <path>` to see whether a later commit already
acted on it. Facts from the shared agent daily log carry **no provenance** unless prefixed
(`app:`/`data:`/`ml:`); re-derive before citing.

A **docstring-only commit** is still a claim: `git show --stat`, then parse both revisions, blank every
leading string constant (`ast.Constant` at `body[0]`) and compare `ast.dump` — equality means no code
moved, but only where the diff is inside existing string constants, because an added `ast.keyword` is
structure blanking cannot remove; a changed call signature needs the hunk read. The **subject** is a
claim too: a `docs(...)` subject licenses "no logic moved", never "nothing user-visible changed",
because `--help` text *is* user-visible behaviour. A **number inside a message is a claim like any
other**: run the command **at the revision you cite** and paste its output, using `--numstat`
(`git show --numstat <sha> -- <path>`, per-file, tab-separated), not `--stat`, since a multi-file
commit's per-file pair cannot be summed as one `+N/-M`. A figure is checkable only when the same
sentence binds it to a sha; a number written with the commit, before the sha exists, can only be
re-derived afterwards.

## customers

| source field | table column | rule |
|---|---|---|
| member code | `member_id` | must match `^M-[0-9]{6}$`, else reject row + quarantine |
| source key | `external_member_id` | unique per source; second-see is update, not insert |
| phone/email | `phone`, `email` | exact-first matching; on an exact miss a bounded substring fallback (min 3 digits / 3 chars, LIMIT 20, POST body, criteria-type-only audit) |

## orders

| source field | table column | rule |
|---|---|---|
| store code | `store_id` | NOT NULL; resolve via store map, create store if whitelisted |
| source order key | `external_order_id` | idempotency key; replay = no-op |
| demo seed | `source='demo'`, `external_order_id='DEMO-O-NNN'` | stable keys, so re-seeding `db/seeds/demo.sql` inserts nothing (the item rows are gated on the order having no lines) |
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
Daily KPI job: `python scripts/run_daily_snapshot.py --as-of YYYY-MM-DD`
— it writes **one `kpi_daily_snapshots` row and nothing else**. The ML
`run_snapshot` / `run_recs_batch` lines in its docstring are sibling
invocations to run separately (then log to `model_training_runs`); the
script never writes `rfm_snapshots`. Model runs are scheduler manifest
jobs — see **Daily RFM writer** below. Queries in
`db/queries/kpi_daily.sql`.
Scope columns (`brand_id`, `group_id`, NULL = group-wide) match 012.

**Snapshot day boundary is business time (Asia/Hong_Kong, +08).**
- `as_of` / `window_from` / `window_to` are business-day dates: a bound named `2026-10-16` is `2026-10-16T00:00+08` = `2026-10-15T16:00Z`. `rfm_snapshots.window_from` is a `date`, so no stored row reveals the offset; a reader assuming `SHOW TimeZone` (GMT on this stack) or the host clock is wrong by up to 8h.
- ML pins the naive->aware conversion (`ml_engine/rfm.py:_day_start`, `ZoneInfo("Asia/Hong_Kong")`); zero stored values change on a +08 host, so no `RULES_VERSION` bump.
- Fixed 2026-10-17 in `pipelines/snapshot.py` (no migration, no schema change): every bucket-deciding trunc carries the offset (`BUSINESS_TZ_SQL`, `pipelines/snapshot.py:144-146`, `AT TIME ZONE interval '{BUSINESS_TZ_OFFSET_HOURS} hours'`).
- `business_today()` (`pipelines/snapshot.py:245`) = `datetime.now(timezone(timedelta(hours=8))).date()` is the default `--as-of` of `scripts/run_daily_snapshot.py:70` and `scripts/run_pipeline.py:144`.
- `BUSINESS_TZ_NAME` keeps the zone name (app pins `TIME_ZONE` in `apps/web/lib/format.ts`, ML pins `ZoneInfo`).
- The defects: `date_trunc($1::text, ordered_at)` ran in the **session** zone (GMT), so week/month cohorts were UTC — over 8,685 orders, 137 month / 628 week / 3,698 day shift; and the `current` cutoff was server `CURRENT_DATE - 1` while both scripts defaulted `--as-of` to host `date.today()` (they disagree 00:00-08:00 local).
- **Cohort figures change by design; old and new are NOT comparable.** Over 940 purchasing customers, 10 month / 62 week / 0 year cohorts move. `cohort_snapshots` was purged and regenerated: its grain UNIQUE (`granularity, cohort_period, period_offset, COALESCE(brand_id), COALESCE(group_id)`, `020_cohort_snapshots.sql:31`) has **no tz component**, so a re-run without the purge would mix UTC and +08 generations in the heatmap. Post-fix: offset-0 `cohort_size` == +08 recomputation for all 36 month / 152 week / 4 year buckets, 940 members each.
- Server-zone exception, one CHECK: every stored date is passed as an explicit `$1::date` parameter, except `db/migrations/017_customers_demographics.sql:11 CHECK (date_of_birth IS NULL OR date_of_birth <= CURRENT_DATE)`, evaluated in the **server** zone (a today DOB is then a violation 00:00-08:00 HKT). Deliberately not tightened — a false rejection of a newborn's DOB is worse than a one-day-future value.
- Environment trap: this pgembed install ships **no tzdata**, so `AT TIME ZONE 'Asia/Hong_Kong'` raises `time zone "Asia/Hong_Kong" not recognized`; any SQL-side pin must use the explicit offset form (Hong Kong has no DST).

**Which `as_of` default do you get?** Read the call site, not the column.

| source (default when `as_of` is omitted) | resolves to |
|---|---|
| data `pipelines/snapshot.py:245 business_today()` (+08 fixed offset), used by `scripts/run_daily_snapshot.py:70`, `scripts/run_pipeline.py:144` | +08 business date |
| data scheduler `pipelines/schedule.py:410` in `_with_common` | passes `--as-of ctx.as_of`; `explain_batch` / `assoc_batch` / `matrix_batch` / `recs_batch` declare no `--as-of` (argparse rejects it, `pipelines/schedule.py:568-570`) — always explicit or absent |
| ml `ml_engine/rfm.py:79 business_today()` (`ZoneInfo("Asia/Hong_Kong")`), applied by `ml_engine/rfm.py:123` in `resolve_window(...)` (`:101` def), `ml_engine/store_ld8.py:240 run_ld8(...)`, `_coerce_as_of` of `store_churn.py:217`, `store_clv.py:189`, `store_forecast.py:50`, `store_segments.py:419` | +08 business date |

Guard `tests/test_engine.py:382-408` pins an in-band instant 17:37Z = 01:37 HKT.

**`--as-of` labels a run; it does not anchor a fit.** The OVERALL/current segments fit is always the current rolling-365 business window (`_fit_overall`, `ml_engine/store_segments.py:174-181`; same at `store_explain.py:97-98`), so a run row's `as_of` is an idempotency/version key, not a window key. For a backfill only `forecast` (`as_of` → `forecast_demand`, `store_forecast.py:80-81`) and `ld8` (`resolve_ld8_window(key, as_of)`) derive a window from `--as-of`; churn/clv fit against `datetime.now(timezone.utc)` and segments-overall fits the current window, so re-running an old `as_of` there **relabels** today's fit.

**Name collision — `resolve_window` is two different functions.** data `pipelines/schedule.py:148 resolve_window(mode, n, unit, ...)` = cohort/period window; ml `ml_engine/rfm.py:96 resolve_window(mode, as_of, date_from, date_to, *, max_span_days=731)` = RFM snapshot window (tz-aware `+08`; `max_span_days=None` is the caller asserting a deliberate full-history span, not a clamp). Check the repo before reading either as the other's contract.

## Daily RFM writer: `ld8_snapshot` (v2) vs `rfm_snapshot` (v1)

`rfm_snapshots` has two *scheduled* writers, and the engine name in the manifest decides which one a tick uses (a third, non-scheduled writer is at the end of this section):

| engine | ml job -> module | rules | windows | conflict behaviour |
|---|---|---|---|---|
| `rfm_snapshot` | `ml_jobs.run_snapshot` -> `ml_engine/store_rfm.py` | **v1** (`ml_engine/rfm.py:24 RULES_VERSION = "v1"`) | one, from `params.mode` | **APPEND-only**: `SNAPSHOT_INSERT_SQL` has no `ON CONFLICT` and no find |
| `ld8_snapshot` | `ml_jobs.run_ld8_snapshot` -> `ml_engine/store_ld8.py` | **v2** (`store_ld8.py:36 RULES_V2`) | FOUR: `current` (rolling 365d ending as-of) \| `mom` \| `yoy` \| `lifetime` | find-or-create on (mode, window_from, window_to, window_key, scope), filtered `rules_version='v2'` |

- 031 declares v2 for time-aware rows, so **v2 is canonical for `window_key='current'`** and `ld8_snapshot` is the daily-capture engine; `rfm_snapshot` stays for v1 history/backfill only.
- Trap: if the daily job is left on `rfm_snapshot`, the v1 insert is append-only and the v2 find is v2-scoped, so one v1 and one v2 row land for the same `window_key='current'` window **every day**; `GET /analytics/rfm/movement?mode=rolling_365` auto-selects the two most recent rows by `window_to` and only then rejects a version mismatch, so the default path answers **422 `snapshot_mode_mismatch`**.

**Never pair v1 with v2 for movement.** Same maths both ways (`store_ld8.compute_window` calls `store_rfm.compute_current` verbatim, `ml_engine/store_ld8.py:141-175`), but v1 is an append-only single-window write and v2 a four-window find-or-create, and the *engine dict* decides the version, not the caller. The v1 INSERT takes `rules_version` as a parameter (`SNAPSHOT_INSERT_SQL`, `store_rfm.py:35`; column at `:37`, `computed["rules_version"]` passed at `:179`) — `compute_current` reads the constant at `:153` (`"v1"` at `rfm.py:24`) — while the v2 INSERT interpolates the literal `'{RULES_V2}'` (`SNAPSHOT_INSERT_V2_SQL`, `store_ld8.py:52`, VALUES at `:56`; find twin `SNAPSHOT_FIND_V2_SQL` at `:61`, WHERE at `:64`; `RULES_V2 = "v2"` at `:36`), so cross-version movement is meaningless. Serving fix: take the newest snapshot's `rules_version`, then the two newest **of that version** for that `mode`/`window_key` in scope, returning the existing `need_two_snapshots` 404 when only one exists — never relax the mismatch check.
- Pairing is **same `mode` + same `rules_version` + same `window_key`**; the bounds need not match and normally do not (each tick find-or-creates its own window, so a same-day re-run adds nothing). `window_key` separates windows *within* a mode (`current` vs `mom`/`yoy`/`lifetime`) and never pairs across them: a two-row requirement is always **two nights of the same window**.
- The version term is load-bearing for `rolling_365` (a v1 `current` row sits beside v2 `current` rows) and vacuous in `custom` (no v1 row), because `window_key` **cannot** separate v1 from v2: it is `NOT NULL DEFAULT 'current'` (`db/migrations/031_rfm_time_aware.sql:19-20`) and the v1 INSERT omits the column (`store_rfm.py:35-43`, nine columns at `:37-38`), so the **DDL default fills it** and a v1 `rolling_365` row reads `'current'` exactly like a v2 one.
- **LD8's reachable space is exactly four combinations** (`ml_engine/store_ld8.py`: `WINDOW_KEYS` is the four keys, and `compute_window` maps each to its mode in the if/elif/else at `:153-169` — that function's docstring is the write-site invariant): `rolling_365×current`, `custom×lifetime`, `custom×mom`, `custom×yoy` — **`custom×current` is unreachable from the writer**. The `window_key='current'` pin therefore yields the empty state structurally.

**Switching the job is a manifest edit, and `pipeline_manifests` rows are edited through the app config API, never by migration** (015 seed comment):

    rfm:
      engine: ld8_snapshot
      depends_on: [apply]
      params: {}          # drop `mode: rolling_365`: the LD8 CLI has no --mode
The builder rejects the legacy param with a named `ManifestError` rather than letting the CLI exit 2 on every tick (`pipelines/schedule.py` `_build_ld8`).

**First-tick triage.** The argv's positional DB URL is the runner's resolved `ctx.db_url` (`pipelines/schedule.py:398` emits `[python, -m, <module>, ctx.db_url]`, sourced from `scripts/run_pipeline.py:106-127` via `_connect_resolved`), **never** a manifest `params` value: a wrong row fails at dispatch as a `ManifestError` with a message *above* the spawn, a bad URL is a runner/env fault that takes every ml job in the tick down, and an unset one is caught pre-spawn (`pipelines/schedule.py:395-397`, `UnknownEngineError`). The ml CLI validates its args *before* connecting (`ml_jobs/run_ld8_snapshot.py:20-28`; order-only change, ml `148cced`), so a malformed `--as-of` or scope UUID fails as a `ValueError` with no connect attempted, while a bad URL is a driver error (`exit 1`, e.g. `socket.gaierror` / `ConnectionRefusedError`) that reads as a connectivity incident — a hand-run typo can no longer masquerade as a connectivity fault, and the tick is unaffected either way since it always passes `ctx.as_of.isoformat()` (`pipelines/schedule.py:410`, a `date`).

**`--as-of` is a silent no-op knob on a window that already exists.** On the tick it is always explicit (`_build_ld8` → `_with_common`, `pipelines/schedule.py:457`/`:402-410`). `ld8_snapshot` find-or-creates on `(mode, window_from, window_to, v2, window_key, scope)` (`ml_engine/store_ld8.py:63-66`, the `SNAPSHOT_FIND_V2_SQL` predicate), so an `--as-of` whose `current` window is already stored (e.g. `--as-of 2026-10-16` while the v2 row for `2025-10-16 → 2026-10-16` exists) exits 0 having written nothing: the tick is green, no row is wrong, and a consumer needing a newer period stays on `need_two_snapshots` with nothing to point at. Rule: omit `--as-of` unless you intend a new period; never reuse a date whose window is on disk.

**Serving-side freshness.** The app's `POST /analytics/{churn,clv,segments}/snapshots` calls `store_*.create_snapshot` **in-process with `as_of` omitted** (app `services/api/analytics/snapshots_serve.py:48-54`), so an `ml_engine/` change is live for `ml_jobs/` immediately but **live in the API only after that process restarts** — compare the changed file's mtime against the serving process's start time, not `git log`.

**`created_by` provenance.** `--created-by` is passed through (`scripts/run_pipeline.py:269` default `scheduler`), so via the runner a NULL is **unreachable**. Of the 12 `ml_jobs/run_*.py` entry points, 10 declare `add_argument("--created-by"` and 9 reference a `created_by` column; the 2 that declare nothing (`run_alive_path.py`, `run_matrix_batch.py`) reference none; `run_churn_predict.py:50-52` is **inert** — it accepts the flag and never passes it (`:26` calls `store_churn.predict_one`, `:54` prints JSON) — and no tick can reach it: `churn_predict` is not an `ENGINE_BUILDERS` key (`pipelines/schedule.py:581-594`, ten engines) and dispatch raises `UnknownEngineError` (`:190`, `:800-803`). app settled the other half: `churn_customer_scores` **is** read by app (`db/queries/analytics_customer.sql:26-33`, `analytics_snapshots.sql:31`), but those rows come from `store_churn.create_snapshot` (`:478`; `SNAPSHOT_INSERT_SQL` `:538`, `SCORES_INSERT_SQL` via `executemany` `:548`)
driven by the churn snapshot writer `ml_jobs/run_churn_snapshot.py:23`, while `predict_one` (`:411`) writes nothing (`return out` at `:428`/`:475`) — so removing the dead flag is app-safe as well as scheduler-safe. A NULL in the column therefore dates the row to a **direct ml-CLI hand-run** of one of the 9 writers, or to a tick handed an explicitly falsy stamp: `--created-by ""` is dropped by the truthiness guard at `pipelines/schedule.py:386-387` (again at `:421-422`) before `scripts/run_pipeline.py:132`'s non-empty default applies.
`JobCtx` is constructed only at `scripts/run_pipeline.py:113`. **Pass a staff code wherever a job writes.**
- The hole is **structural, not incidental**: all 12 `created_by` columns in this repo's DDL are `text NULL` with no `DEFAULT` (`db/migrations/007_customer_events.sql:14`, `010_analytics_rfm.sql:15`, `011_recommendations.sql:14`, `013_analytics_history.sql:24,57,115`, `014_upload_staging.sql:39`, `015_segments_pipelines.sql:37`, `018_assoc_rules.sql:25`, `019_connectors.sql:27`, `021_explain.sql:27`, `022_forecast.sql:30`), so an unstamped hand-run **cannot fail anywhere**. Data's own CLI follows the convention (`scripts/run_upload.py:124` `default=None`; the API stamps the session, `services/api/routers/customer_events.py:73`). Making `--created-by` **required** in the 10 ml CLIs was considered and **declined**: a 10-file argv change (`[BREAKING @both]`) that forces every dev/one-off invocation to pass a value, so the rule above closes the hole operationally.

**The list query's window projection.** `SNAPSHOTS_LIST_SQL` (`store_rfm.py:53-66`) projects `window_key` (`:54-55`) and orders `window_to DESC, scored_at DESC, id DESC` (`:64`) — period, not compute time. App consumes it **directly** (`../crm-cdp-app/services/api/routers/analytics_rfm.py:37`), and its `custom` tab pairs the two newest `custom` rows. Trap: `mode='custom'` in `rfm_snapshots` is **LD8's named-window bucket** (`mom`/`yoy`/`lifetime`), not the user's custom date range — the tab's *scores* come from a live computation over the requested dates, its *band* from that bucket (the stored `custom` rows are all v2: `lifetime` `807d4f38` / `2895ba93`, `mom` `880c0cbe`, `yoy` `8950aaec`). What is wrong there is **label vs window**: the band prints run dates, not bounds (`formatDate(movement.from.scored_at) → formatDate(movement.to.scored_at)`, `apps/web/app/rfm/page.tsx:501`, also `:551-552` — a segment-chart *axis* — and `:624`), so a bounds-based fix is a three-site edit and a chart-semantics change, not a one-liner.

**Successive-period rule, branched by what the page displays.** A *mode-defined* window (`rolling_365`, `year_to_date`) tests **translation** — equal deltas at both ends equal to the cadence (the `rolling_365` pair is 1d/1d, accepted; the `custom` `lifetime` pair grows from the 2023-11-12 floor, 0d/1d, rejected because it *grows* rather than shifts). An *explicit range* (the custom tab's `date_from`/`date_to`) tests **equality with the request** — identical bounds, different `scored_at`; translation alone accepts any two same-length ranges in different months. The `window_key='current'` pin stays in both branches, yielding the empty state structurally.

**POST path.** `SNAPSHOT_INSERT_SQL` (`store_rfm.py:35-43`) omits `window_key` from its column list (`:37-38`, nine columns), so the DDL default stamps `'current'` on every app-POSTed row **regardless of mode or user dates**; the `RETURNING` names it too (`:40-42`), so a POSTed row comes back **self-identifying** (`mode='custom'` *and* `window_key='current'`) with no schema change and no extra request, and two POSTed `custom` snapshots for different ranges would both be `current`. `window_key`'s job here is in the **guard**, not the predicate: the movement guard checks `mode` + `rules_version` only (`analytics_rfm.py:265-270`), so a cross-key pair is accepted with 200 — proven live `from=880c0cbe` (`mom`) → `to=807d4f38` (`lifetime`) → `customers_both 252, entered 688, exited 0`, which is the window widening, not movement. Scope that fix to this endpoint: LD8's own `segment_movement` pairs different keys **by design** (`mom`→`current`; `ml_engine/segment_movement.py:23-26` `PAIR_WINDOWS`).
- `store_rfm.py:36` and `store_ld8.py:53` are the only production writes of `rules_version` (the app POST reuses the former; its only direct INSERTs are in `../crm-cdp-app/services/api/tests/test_rfm.py:451/513/537`), so a scope holding **only v1 rows** is exactly a scope **LD8 has never been ticked in** — check `SELECT DISTINCT rules_version` for that scope; the remedy is a scoped LD8 run, not an empty-state fix.

**The four `snapshot.py` statements are effectively frozen.** ml `tests/test_ld8.py::test_031_statements_still_match_data_runner` reads this repo's `pipelines/snapshot.py` (via `DATA_REPO_PATH`, default `../crm-cdp-data`), extracts the four statements by name and asserts **token**-equality — `_norm` collapses every run of whitespace, so reformatting passes and only a token change fails (`tests/test_ld8.py:43`) — and asserts the whole `*_SQL` name **set** — keyed by *name shape*, not value shape, so no constant form escapes the extractor (`_data_sql_names`, `tests/test_ld8.py:103`; the frozen expectation `_DATA_SQL_NAMES`, `tests/test_ld8.py:124`, eleven names: the four mirrored plus seven data-only, `BUSINESS_TZ_SQL` among them), so an **addition** fails ml's suite too, naming the missing constant and its source (`tests/test_ld8.py:157`); the skip is the absent sibling checkout (`@pytest.mark.skipif`, `tests/test_ld8.py:139`). Data's four constants are `pipelines/snapshot.py`'s `SEGMENT_SIZE_UPSERT_SQL`, `MOVEMENT_UPSERT_SQL`, `SIZES_FOR_WINDOW_SQL` and `MOVEMENT_FOR_PAIR_SQL` — the **names are the pin**, and a line number is worse than illustration: read at `ac39625`, all four sit 12 lines lower than the 373/380/387/394 this file used to quote, and not one byte of them changed — and **data's** `crm-cdp-data/services/api/tests/test_rfm_movement.py:78-81` imports all four and executes them against a migrated `031` (`:123`/`:146`/`:149`/`:157`), so a **grain** change fails data's suite at the change site — and only data's *can*: ml's `pytest tests` runs without `DATABASE_URL` by design (pure-engine suite), so its reachable half is text-only, and its guard **skips** entirely from a checkout with no sibling `crm-cdp-data` (`tests/test_ld8.py:139`). A change to these four statements' text or grain is an in-room `[BREAKING]` notice to crm-ml, not a repo-only edit. App has no equivalent check — `../crm-cdp-app/db/queries/rfm_windows.sql` mirrors the two read statements by hand (app names `sizes_for_window` `:11`, `movement_for_pair` `:17`) and app's `services/api/tests/test_rfm_windows.py` exercises the endpoints over rows it seeds itself (`_seed_windows` `:74`), importing none of the four names — so app's mirrors lag and fail *worse*: self-contained SQL keeps serving an old shape with no error anywhere, and ml's test compares *data's* statements, not app's.

**`m_cap_pct` is a live-request knob, never a stored property.** The cap is an engine kwarg (`ml_engine/rfm.py:262` declares it, applied `:278-279`; forwards to `store_rfm.py:115/140`, `store_ld8.py:157-174`) reachable from the app's two live GETs only (`../crm-cdp-app/services/api/routers/analytics_rfm.py` params `:115`/`:279`, `Query(ge=0.01, le=1.0)`, passed straight to the engine at `:125`/`:290`); the scheduled writer always uses `DEFAULT_M_CAP_PCT = 0.99`, and no renderer emits a flag for it (the `m_cap_pct: (False, None)` entry `rfm_snapshot` carried was a declaration nothing could turn into argv; removed). Do not add a `--m-cap-pct` flag, and do not pin the GETs: `rfm_snapshots` stores no cap and no params (`db/migrations/010_analytics_rfm.sql:6-16`), so a caller-chosen cap row would be unattributable from storage, while a live response **self-attributes**, returning the cap it used (`"m": {"m_cap_pct": …, "winsor_cap_cents": …}`, `ml_engine/rfm.py:317`, validated `:292-295`). The write path is **pinned by decision** (app, read at `6bdf2b4`; guard `_checked_body` at `analytics_rfm.py:55-67`, applied on the POST at `:140`): the body default is the engine constant (`:52`); a non-default cap is rejected by name — `422 {"code": "non_default_m_cap_pct", "default": engine.DEFAULT_M_CAP_PCT}` (`:61-64`) — and any cap outside `(0, 1]` with `bad_m_cap_pct` (`:59-60`). Cap experiments belong on the live endpoints, which persist nothing.
- Edge 1: the cap is consulted **only on the quintile path** — `score_rfm` validates and applies it in the `else` branch alone (`:292-295`, reported `:317`), so a population below `QUINTILE_MIN_POPULATION` (`:25`, 25 rows) takes `_fallback_scores` (`:289`) and is stamped `method="threshold_fallback"` (`:290`): the cap is never read, and that path's `m` boundaries are median-relative with no `m_cap_pct` key. **Scope caveat:** the cap statement above is therefore honest only on the quintile path — irrelevant at **920 customers** (the live population), but it bounds the app refusals too, which guard a parameter the engine may not consult.
- Edge 2: `DEFAULT_M_CAP_PCT` (`:26`) is covered by **no version string** — `RULES_VERSION = "v1"` (`:24`) and `RULES_V2 = "v2"` (`store_ld8.py:36`) version the segmentation ruleset, not the cap — so changing that constant is a **contract event (@both)**, not a refactor: every stored row's M cuts would lose their stated derivation and no column records it.

**Scope is a run-level dimension, so no brand-scoped snapshot row exists.** `--brand-id`/`--group-id` (`scripts/run_pipeline.py:267-268` → `JobCtx`, `:124-125`) reach **every** job of a tick via `_scope_args(ctx)` (`pipelines/schedule.py:380-388`) and `_with_common` (`pipelines/schedule.py:410-423`, `with_scope` default True): per-brand rows require **one runner invocation per brand**, and that invocation scopes the *whole* pipeline, not one job. The nightly cron passes neither flag, so every stored row is hq-scoped (`brand_id IS NULL`) — correct. A brand-scoped session then gets live RFM at `n=435` while `GET /analytics/rfm/snapshots` returns **0 rows** and `movement` a **404 `need_two_snapshots`** — **not a platform limit: nothing writes scoped rows and no UI action calls the POST**, while a scoped POST works (`services/api/tests/test_rfm.py:380` asserts `201`). Find-or-create is scope-aware (`ml_engine/store_ld8.py:65-66`), so a per-brand fan-out would add rows beside the hq row; its price on the demo dataset is **6 ticks**, one per brand (`DEMO`/`SUNHING`/`KVLN`/`VICEW`/`HARBOR`/`PEAKLENS`), because the single group makes a `--group-id` run customer-identical to hq. No ML change is needed (`ml_engine/store_rfm.py:29-31`; `run_ld8` already threads `brand_id`/`group_id`), so per-brand writes are a **runner** decision — and they do not fix the scoped role that night: the band pairs **two** `current` rows of the newest `rules_version` in scope, so each scope needs a second scoped night, and the accurate copy is scope-neutral (*"no movement for this scope yet — movement appears once two snapshots of this window exist for this scope"*), because "the view needs a second night" is false of a scope whose own row count never moves.

**Group scope is conjunctive by design, and its only silent exclusion is pre-group customers.** A group run requires `customers.group_id = g` **and** `brands.group_id = g` (`db/migrations/011_groups.sql:6` — customers shared within a group with NO linkage across clients, a hard silo). `brands.group_id` is NOT NULL (`:23`); `customers.group_id` is NULLABLE on purpose (`:86`, the same legacy pattern as `consents.brand_id` / `NULL = pre-brand`, `:95-99`), so a group-scoped run **excludes** those rows silently. On the live demo that half is a no-op (1 group `DEMO`, 6 brands, **0 of 1208 customers with a NULL `group_id`**), so it is a contract property, not a testable one, and it has **no coverage in either suite** (app's group tests at `services/api/tests/test_groups.py:90`, `:133`, `:155`, `:217`, `:287`, `:331` never put a NULL-`group_id` customer under a group run). The switch that would **include** those rows instead of dropping them is the `IS NOT DISTINCT FROM` form — **declined on purpose, not missed**: 011's design is exclusion, and a group run means the group's pool, not "the pool plus unassigned rows".

**`IS NOT DISTINCT FROM` locates a stored artifact by its recorded scope; the `IS NULL OR` guard scopes a live population.** The two forms must not be confused, and they fail in opposite directions. The population form's NULL test is on the *parameter* (`$3::uuid IS NULL OR …` = "no restriction on this column"), so a **stamped** surface using it would **disclose** `NULL/NULL` rows into a brand session — a visibility bug; the artifact form compares *column to parameter* NULL-safely (`IS NOT DISTINCT FROM …` = "only rows whose stamp is NULL here") and can only **hide**, which is this system's quarantine default. The rule is **two-step**: locate the row whose visibility the predicate decides — a **stamped** row takes stamp-equality on its scope stamp, a live or joined source row takes the population form even when the column in hand is called `brand_id` — then ask what the column *is on that row*: a scope-stamp column of the located row takes `IS NOT DISTINCT FROM` (a NULL there means "computed globally" and must fail closed), a non-scope attribute of that row or a dimension of a joined row takes `IS NULL OR` (a NULL there means "no restriction").
- One statement, one row, both forms: `SNAPSHOTS_LIST_SQL` `store_rfm.py:57 WHERE ($1::text IS NULL OR mode = $1)` is the population form for `mode` (NULL = every mode), `:58-59` the artifact form for the scope stamp (`:56` is only `FROM rfm_snapshots`). One file, the same two scope columns, opposite operators: `store_churn.py:193-198` (`SNAPSHOT_FIND_SQL`, artifact, guarding `:196-197`) vs `:253-261` (`CUSTOMER_TX_ORDERS_SQL` / `CUSTOMER_TX_ACTIVITY_SQL`, population, guarding `:258-259`, `:263-281`). File-level: `analytics_snapshots.sql:22-23` (artifact, stamped row) vs `:110-111` (population, joined `b.id`/`g.id` dimensions) — which kills a per-file label.
- All **11** `IS NOT DISTINCT FROM` predicate pairs in `ml_engine/` are the artifact form (`store_churn.py:193-198`, `:301-302`, `:311-312`, `:323-324`, `store_clv.py:168-169`, `store_forecast.py:31-32`, `store_ld8.py:65-66`, `store_recs.py:88-89`, `:103-104`, `store_rfm.py:58-59`, `store_segments.py:388-389`); the guard's *reads* are the population form (`store_churn.py:253-261`). The aggregate filter is population form — `AND ($4::uuid IS NULL OR c.group_id = $4::uuid)` (`ml_engine/store_rfm.py:30`; the guard form is three lines `:29-31`, and the last two guard the **same** `$4`, so a customer whose own group differs from their store brand's group is dropped by the brand line) — whereas the *listing* query uses `brand_id IS NOT DISTINCT FROM $3::uuid` / `group_id IS NOT DISTINCT FROM $4::uuid` (`store_rfm.py:58-59`). Both shorthands are wrong alone: the column *name* is not the rule (`brand_id` sits under both operators at `store_rfm.py:29` vs `:58`), and the row's *kind* is not the rule (one row can carry both forms).
- App's reference decision is `../crm-cdp-app/services/api/analytics/scope.py:1-51`: a stamped row is visible iff `row.brand == session.brand AND row.group == session.group`, NULL-safe on both sides, with **quarantine-by-default** — `NULL/NULL` legacy and global rows are **HQ-only**, so an unstamped write is not an error but a row only HQ can see (`row_visible`, `scope.py:62-67`). Because the predicate is **mutual**, a brand-less HQ session `(None, None)` matches only `NULL/NULL` rows, so brand- and group-stamped rows are invisible to HQ **by construction** (`scope.py:21-25`) — deliberate, not a defect. Where the intent really is "this session's rows *or* the global ones", the population form cannot express it; app writes the disjunction out explicitly as an **added term**, never a relaxed operator (`scope.py:27-30`). `session_scope` returns `(brand, None)` or `(None, group)`, which keeps a group-stamped `(NULL, G)` row out of a brand session. App's population-form hits are all source-row filters over `orders`/`customers`/`brands`/`groups` (`analytics_snapshots.sql:110-111`, `analytics_simulate.sql:16-18`, `analytics_recs_fallback.sql`, `stats.py:23-29` `_ORDER_SCOPE`/`_CUST_SCOPE`) — none is a stamped surface, so no leak. So "admin can't see the brand runs" is a **visibility-policy** question for the contract and the owner, never a WHERE-clause tweak in a migration.

**Third writer, non-scheduled:** `POST /analytics/rfm/snapshots` writes **v1** through the same `store_rfm.create_snapshot`, stamped `created_by=user.staff_code` (`analytics_rfm.py:132-142`). It is an ordinary v1 append: it changes the v1 row inventory, not the pairing rule above, and with the cap pinned it cannot store a fit the v1 engine path could not express.
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
   the insert itself is `ON CONFLICT DO NOTHING` on that same partial
   UNIQUE, so a row that lands between the pre-check and the insert (a
   concurrent apply) is skipped and counted instead of failing the whole
   job; events pre-check `(source, source_event_id)` with the same
   conflict-ignore on insert; identity binds the
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

Segments k: the seed pins `k: 5` EXPLICITLY on every `engine:
segments` job, because serving reads the LATEST run for (mode, k,
scope) and its own default is `k=5` (app segment/explain routers,
`k: Query(ge=2, le=20) = 5`). An unwritten k is not a soft miss: the
read answers 404 `no_snapshot` with `available_k`. A cadence that only
ever fits k=6 therefore leaves the product's default surface dark
right after a wipe while every job reports succeeded -- and the
product exposes exactly ONE k (no picker;
`apps/web/app/segments/page.tsx:561-564` — the file's own comment says
"No k picker", so the claim and the cite agree),
so one k is enough and a miss is answered with guidance built
from `available_k`. Extending to a ladder (e.g. {5,6}) is a manifest
edit if a second k ever gets a consumer, not a code change. Do NOT
"serve the latest available k" instead -- that relabels k=6 clusters
as the k the caller asked for.

k is a CALLER PARAMETER with a configured value, never a model choice:
no engine selects k (no elbow/silhouette anywhere in ml -- the fit
computes `inertia` for whatever k it is handed, and `store_segments`
records `k_requested`/`k_used`). The page copy's "fitted by the model
(elbow/silhouette)" states intent (staff do not choose k), not
mechanism; do not "resolve" that wording by adding auto-k selection,
which would fight the pin. All four places name the same value: the
015 seed, the runner default, ml's CLI default (`66fe0c7`), and the
live `pipeline_manifests` rows.

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
That ownership is a **split, not a to-do for ml**:
`pipeline_manifests.schedule` is a *declaration* whose 5-field shape ml
validates (`ml_jobs/manifest.py:141-142` `is_valid_cron` — "Light 5-field
cron shape check. The runner owns full parsing.", called only at
`:189-190`/`:218-219`) and never evaluates, and ml has no scheduler entry
point at all (every entry is a one-shot `python -m ml_jobs.run_*` that
connects, writes, exits — no loop, no daemon). So wiring a trigger is a
change to **this repo's runner only, zero ML change**. The **feature**
half is still open (015 gives manifests + `pipeline_runs`, but nothing in
the product schedules a tick or shows one — app, after 015). The
**infrastructure** half shipped 2026-10-17 as `[ADDITIVE]`:

### The in-repo tick: `--watch` / `--once` `[ADDITIVE]`

`scripts/run_pipeline.py --once` (one pass — the unattended entry point)
and `--watch [--interval 60]` (that pass in a loop): each pass reads
`pipeline_manifests` and fires every row whose `schedule` matches the
business clock (`pipelines/schedule.py` `cron_matches` — 5-field Light
cron; Sunday `0` or `7`; `dom`/`dow` **ORed** when both are restricted,
Vixie semantics; a field it cannot read raises `ManifestError`, logged
per row, never skipped silently). `--only a,b` filters rows; `--now` pins
the clock (dev/verification; naive = `Asia/Hong_Kong`).

- **The stored cron is a declaration and the tick is one evaluator of
  it, never the authority.** Any trigger calling `--once` at least once a
  minute is equivalent (Task Scheduler, cron, systemd timer), and a
  competing trigger is safe — the runner skips, below. A host cron that
  bypasses the table (the `--pipeline <name>` entrypoint) is still a
  **hand-run**, not a tick: its rows carry no `upstream.tick`.
- **Attribution.** Each fire gets one id (`uuid4().hex[:12]`) stamped on
  every row it writes as `upstream.tick`. 015 `pipeline_runs` has no
  actor column, so this is what the table can hold: tick-written rows are
  distinguishable from hand-runs, rows within one fire are not. The stamp
  is opt-in on `JobCtx.tick` (default `None`), so `--pipeline` rows are
  byte-identical to what they were before this change.
- **Idempotence is the runner's, not the loop's.** `pipeline_runs` is
  unique per (pipeline, job, `as_of`, scope) and a `succeeded` row skips
  unless `--force`, so a second tick for a window already succeeded writes
  nothing new — overlapping triggers, a restart and two hosts converge.
  A row that is `running` refuses the claim (`UPSERT_RUN_SQL` returns no
  row), so two cursors in one cron minute cannot execute the same job
  twice: the loser skips that job *and* its dependants (outcome carries
  `blocking`), and a `running` row older than `RUN_STALE_AFTER` (2 h) is
  reclaimed, so a killed tick does not wedge the job forever.
  The loop's `fired` set only stops one process from re-entering the same
  window inside one `as_of`; a `failed` row is retried once per process
  start, not every interval (a failure is a row carrying `error`, not
  something to hammer).
- **Every pass is one JSON line on stdout** (`event`: `pass`, `fired`,
  `fire failed`, `bad manifest`, `row name differs from its yaml`, `no
  database`, `once: done`): the log is the loop's only record, and a loop
  that logs only its fires is indistinguishable from one that died.
  SIGINT stops the loop, logs it, exits `130`, and leaves an in-flight
  job's row `running` with no `finished_at` — the next tick retries it
  (skip needs `succeeded`), so an interrupted window self-heals and that
  row is the evidence.
- **Not built:** no daemon or supervisor, no catch-up for windows the
  process missed while down (a missed `as_of` is a gap, not a backfill —
  re-run it explicitly), and one namespace per invocation (scopes still
  fan out as one run per `--brand-id`/`--group-id`).

- **Cites into this file move when the file grows.** The numbers above were
  repinned 2026-10-17 (+117 before `:752`, +122 after): the job's dispatch
  `try`/`except` was cited `:634-641`/`:658` from `5f91e16` (right then) and
  was still *in bounds* after `4ff0910`/`fb2f0a7` shifted it -- a range cite
  that names no symbol on its own line is invisible to every check we have,
  so derive the number, never inherit it.

Do not read a green `pipeline_runs` row as proof a scheduler exists; that
holds for rows without `upstream.tick`, which are hand-runs.

**Run provenance** (ADDITIVE, no migration): a job that ran as an ml
subprocess carries `ml_repo_sha` (full 40-char HEAD of the `--ml-repo`
checkout) and `ml_repo_dirty` (bool) inside `pipeline_runs.upstream`,
resolved once per pipeline run (`git -C <repo> rev-parse HEAD` plus
`status --porcelain`; best-effort — git absent or the path is not a
checkout gives `null` for both and the tick continues, so an unlabelled
row means "could not resolve", never "old row"). Data-engine rows keep
`{"engine": ...}` alone **on purpose**: a revision on a row whose numbers
came from in-process data code is a mislabel. This labels the *tree the
engines were executed from*, which is a different question from ml's
per-file `as of <sha>` pins (last commit touching the cited file, e.g.
`ml_engine/store_ld8.py:70-72` naming `ac39625` while the checkout's HEAD
is ahead of it): the checkout is not a submodule and no revision is
pinned anywhere, so `ml_repo_dirty = true` means that run has **no**
revision at all — the sha is a starting point for `git diff`, not a
reproducible input. app's `/api/v1/pipelines/runs` already returns
`upstream` untouched (`db/queries/pipelines.sql` `runs_list`, parsed by
`_run_to_dict` (`services/api/routers/pipelines.py:105-122`)), so a consumer needs no API
change. The sha is the checkout's HEAD **at run time**, never the last
commit to touch some file (`git log -1 --format=%h` over a path answers a
different question and is not a substitute for it), and it is written on the
**failure** path too: the label is resolved before the engine is invoked and
both finish paths persist it (`pipelines/schedule.py:828`, `:852-863`, with
`error` carrying the other half), because a failing run is exactly when the
revision is worth knowing — `test_runner_labels_a_failed_job_too`
(`services/api/tests/test_schedule.py`) pins that alongside the resolved,
dirty and unresolvable cases.

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

Write/trigger API [ADDITIVE] (`services/api/routers/connectors.py` — all
**six** mutations carry `Depends(require_admin)` (`Admin` at `:60`; the
three GETs are not gated; count the signatures, not the prose — that
docstring's numeral was accurate until the LD2 schedule route arrived and
stayed at *five* after it, until app `327a48b` replaced the count with the
rule, "Every mutation below is admin-gated", which cannot drift again) —
with its data-access module
`services/api/repos/connectors.py` **below** the auth layer, where a gate
would be the wrong place and is absent by design; secrets + execution
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
(`queued|running|succeeded|failed`) are stamped by the **sync worker**,
not by the serving layer: `pipelines/ingest/connector_sync.py`
`note_sync_result(run, status, counts)` closes the run row AND the
connector checkpoint in one transaction (raises on bad status/counts
or missing run). The API only reads the checkpoint — `GET
/connectors/{id}` renders it, `PUT /{id}/schedule` touches
`schedule_cron` alone. App made the checkpoint **worker-owned** in its
connector pass, which is why the writer is a pipeline module rather than
a `services/api/repos` one: data's `4fa0cc3` had landed it inside the API
copy of an app-owned file, and app's later edit dropped it there. The API
still enqueues the run (`POST /{id}/sync` → `queued`); everything after
that is the worker's. Incremental-extract helpers (no I/O): optional
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

That containment is the whole dispatch, not just the resolver: the
resolve call and `ENGINE_BUILDERS[engine](...)` both sit inside the job's
`try` (`pipelines/schedule.py:852-856`: the resolve call is `_resolve_segment_run_id`), so a malformed manifest
(a wrong `params` key raising in `_build_matrix`, an `argv` builder
refusing its input) also lands as `status = 'failed'` with
`type(exc).__name__: exc` on that job's `pipeline_runs` row (`pipelines/schedule.py:858`)
and the tick carries on with the remaining jobs. Consequence for
debugging: a wrong key is **discoverable, not dark** — the reason is on
the run row and in the tick's `outcomes[<job>]['error']`, it just has
no serving-time symptom, so no API response will ever point at it. Query
`pipeline_runs.error` (or the tick result) before assuming a job never
ran.

CLI (ml, `ml_jobs/run_matrix_batch.py`): `python
ml_jobs/run_matrix_batch.py DB_URL --segment-run-id UUID`. Argparse
declares nothing else, so the runner passes exactly DB_URL +
`--segment-run-id` — **no `--as-of`, no `--brand-id`/`--group-id`, no
`--created-by`**; a flag the CLI does not declare is an exit-2 on every
tick. (Read the "no `--created-by`" separately from the scope clause —
scope and `created-by` are independent, and `explain_batch` differs on
the second only.)

**What a job's argv may carry is per-engine, and the split is what a
*row* can carry.** On a scoped tick
(`services/api/tests/test_schedule.py:495-504` pins all three):

| engine | receives `--brand-id`/`--group-id`? | receives `--created-by`? |
|---|---|---|
| `explain_batch` | **no** — scope rides the resolved segment run (`pipelines/schedule.py:543-561`, `_with_common(..., with_as_of=False, with_scope=False)`) | yes |
| `matrix_batch` | **no** — same reason | **no** (argparse declares none) |
| `assoc_batch` | **yes** — reads the catalog directly and no row carries a scope for it (`pipelines/schedule.py:527-540`, `with_scope` default) | yes |

Rule, and the reason a blanket sentence about it would be wrong: scope
arrives as argv only for a job that reads the catalog directly; a job
that reads a resolved **row** (`segment_runs`) takes its scope from the
row, and an argv copy would be a second source of truth that can
disagree with the run it actually read (ml's review catch, fixed
`bce2721`). Exit codes are a signal (taxonomy confirmed by ml): 0 = success
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
`current_x_mom`|`current_x_yoy`, n + pct_of_from). Both pairs are written by
the **same run** from its own window assignments (`ml_engine/segment_movement.py:22`
`PAIRS`, `:23-26` `PAIR_WINDOWS` = `("mom", "current")` / `("yoy", "current")`,
`store_ld8.py:255-263`), each row stamped with that run's `as_of`: one night
populates movement, and a second *date* is needed only for a second
`current` (and `lifetime`) **snapshot** — what the app's same-window diff
pairs — never for the cross-window pairs. There is no `lifetime` pair;
`lifetime` is a window, not a comparison. ENTERED (only
as from) / EXITED (only as to) sentinels for newcomers/exited —
uppercase, never collide with lowercase segments; pct_of_from NULL
enforced (outside the flow pct denominator by construction).
Lesson: `window` is a PG reserved word — column is `window_key`.
ml executes the movement math verbatim via `validate_movement_row`
/ `validate_segment_size` + the UPSERT/SELECT SQL. **Scope:** both aggregates are **unscoped outputs** — no scope
column, no parent FK, global by construction (see **Unscoped outputs** below).

## 037: backtest outcomes (M2, [ADDITIVE] analytics, joint with ml)

`037_backtest_outcomes.sql` (+ `038` grant re-statement, `039` read indexes).
One row per customer per model per holdout window, metrics `auc`/`spearman`/
`calibration_ratio` beside the realized outcome they were scored against.
Column names are the shipped ones — `freq`/`holdout_days`/`predicted`; ml's
prototype `frequency_model`/`horizon_days`/`predicted_value` lost because the
migration landed first and `ml_engine/backtest.py:117-123` was fixed to match
(`858bd98`, and a test now pins the list). **Append-only, in two parts:** the
unique key is the *window identity* (`COALESCE(brand_id, group_id)` +
`model, anchor_as_of, holdout_days, customer_id`), which stops a duplicate row
— immutability is the `REVOKE UPDATE, DELETE … FROM PUBLIC` (027/029 shape),
and a cluster that recorded 037 before that line existed gets it from `038`
(`035` reasoning: dbmate skips a recorded version, `REVOKE` is idempotent;
regression guard `services/api/tests/test_migration_drift.py`). **ml writes
this table and never reads it** (`grep -rn backtest_outcomes ml_engine/
ml_jobs/` matches only the INSERT), so `039`'s read index has no consumer
yet: the per-window read it is measured against (0.575 → 0.268 ms live,
2026-10-19) belongs to app or a later report — do not wait on ml for a
read-side number, and the half-table window aggregate keeps its seq scan by
measurement (0.50 vs 0.45 ms). **Scope:** carrier (below).

## The stamped set: which relations carry a scope stamp (app+data+ml, 2026-10-17)

A **stamped relation** carries a scope stamp (`brand_id`/`group_id`) on the row itself or inherits exactly one through
its parent artifact: the set `row_visible` decides on (`../crm-cdp-app/services/api/analytics/scope.py:1-51`, reference
`:72-76`), and the set the artifact-vs-population rule above applies to. **Derive the group from the schema, never from
the name.** Over `db/migrations/*.sql` `migrate:up` bodies only: **23 = 16 carriers + 7 negatives**; never union the
`migrate:down` direction (it drops `012_analytics_scope.sql:26-32`, `031_rfm_time_aware.sql`).

**Visibility (`row_visible`).** Visible ⟺ NULL-safe stamp-equality between the row's own stamp and the reader's scope
(`row.brand == session.brand AND row.group == session.group`; session = brand `(brand, None)`, group `(None, group)`,
HQ `(None, None)`). The equality is **mutual**: HQ matches only NULL/NULL rows (legacy pre-scope rows, admin-created
global batches), and a group manager sees group-stamped runs but not brand rows. Never relax it to the population form
to "let admin see brand runs" — a brand session would then match global/legacy rows; for "own rows or global" write the
disjunction.

### Carriers — 16
The row's **own** stamp decides visibility: `rfm_snapshots`, `rec_batches`, `clv_snapshots`, `churn_snapshots`,
`kpi_daily_snapshots`, `cohort_snapshots`, `segment_runs`, `assoc_batches`, `explain_batches`, `forecast_batches`,
`model_training_runs`, `pipeline_runs`, `connector_sync_runs`, `upload_jobs`, `btyd_params`, `backtest_outcomes`
(037; `ml_engine/backtest.py` writes it, nothing reads it yet — a future reader owes it the stamp predicate).

### Inheritors — 14
**No scope column of their own**: visibility follows the parent's stamp through an FK, so a scope predicate on an
inheritor can only be evaluated by **joining the parent**. The parent→child edges are the whole list:
- `rfm_customer_scores` ← `rfm_snapshots` (`snapshot_id`); `churn_customer_scores` ← `churn_snapshots`;
  `clv_customer_scores` ← `clv_snapshots` (all `snapshot_id`)
- `segment_assignments` ← `segment_runs` (`run_id`); `cluster_feature_matrix`, `cluster_feature_matrix_long`,
  `seg_surrogate_fits` ← `segment_runs` (`segment_run_id`) — the two `cluster_feature_matrix*` relations are **one
  logical matrix** (`031` `layout IN ('wide','long')`; `032` melts the grain the wide PK cannot hold), so a guard
  covering one misses the other
- `assoc_rules` ← `assoc_batches`; `explain_rows` ← `explain_batches`; `forecast_rows` ← `forecast_batches`;
  `rec_items` ← `rec_batches` (all `batch_id`)
- `landing_receipts`, `quarantine_rows`, `staging_rows` ← `upload_jobs` (`upload_job_id`)

**A group is a property of the edge, not of the child.** Five children carry a second, non-scope parent edge
(`rec_items`, `segment_assignments`, `rfm_customer_scores`, `churn_customer_scores`, `clv_customer_scores` reference
`customers` via `customer_id`), and `landing_receipts` a second optional parent (`manifest_pipeline →
pipeline_manifests`, `027_landing_receipts.sql:27-28`); key a guard on the **edge**, not the table.

Thirteen of the fourteen edges are `NOT NULL … ON DELETE CASCADE`; the exception is a boundary case:
`landing_receipts.upload_job_id` is `uuid NULL UNIQUE … REFERENCES upload_jobs (id) ON DELETE RESTRICT`
(`027_landing_receipts.sql:16-17`), because exactly one of it and `vendor_batch_id` must be set (`:31-33`
`CHECK ((upload_job_id IS NOT NULL)::int + (vendor_batch_id IS NOT NULL)::int = 1)`), so it inherits on the
**file-upload** path only: a **vendor batch import** receipt has no parent artifact, no scope column and no join to a
stamped relation (unscoped by construction), and a guard must skip on the **edge**, not the table — with it empty that
`CHECK` alone holds the invariant, so an empty read proves nothing.

### Negatives — 7
Live entities carrying a scope column that is **not** a visibility stamp: `brands` (`group_id`), `customers` (`group_id`),
`stores` (`brand_id`), `staff` (`brand_id`), `membership_tiers` (`brand_id`), `campaigns` (`brand_id`), `consents`
(`brand_id`). A predicate over these is an ordinary filter over live rows.

### Unscoped outputs — 3
Two shapes, split by what the relation stores:
1. **Holds scope-dependent results with no scope column** — readers must gate, writers must abstain:
   `rfm_segment_sizes`, `rfm_segment_movement` (031; writer ml; the writer choice is **B′**, below). No predicate can distinguish a per-scope row from a
   global one.
2. **Holds no results at all** — no obligation either way: `report_runs` (app-only,
   `../crm-cdp-app/db/migrations/033_report_runs.sql`; data carrying no 033 is the bootstrap-subset rule, not drift) is
   a scope-free **ledger** with no result column. Grouping it with shape 1 invites the one wrong fix available here:
   scope columns on a table that carries no computed content.

**Their writer choice: B′, decided 2026-10-18.** Nothing links the two 031 aggregates to a scope: the writers `SEGMENT_SIZE_UPSERT_SQL` and `MOVEMENT_UPSERT_SQL` (`pipelines/snapshot.py`) are `ON CONFLICT … DO NOTHING`, the readers `SIZES_FOR_WINDOW_SQL`/`MOVEMENT_FOR_PAIR_SQL` filter `as_of` +
window/pair only, so their rows are **global by construction** (first-writer-wins). A scoped
LD8 run still writes its scope-stamped snapshots/scores and **skips** both scope-free 031 aggregates
(`rfm_segment_sizes`, `rfm_segment_movement`) — `run_ld8` is the writer half
(`ml_engine/store_ld8.py`: `write_sizes`/`write_movement` sit behind `if not scoped:`) — and the skip is
**reported as skipped**, never a bare zero: the job returns `sizes: null`/`movement: null` plus
`skipped: [{table, reason}]` built from `SCOPED_SKIP_TABLES`, since "nothing changed" and "not written for this
scope" are indistinguishable to a reader. The rejected alternative **A** (a scoped run writes them anyway) would
**make** a brand's aggregates the global rows for every unscoped read, and `defer` leaves that hole open. The reader
half stays (`../crm-cdp-app/services/api/routers/analytics_rfm_windows.py:1-24`: scoped sessions 403): it closes only
reading, which is exactly why the writer half carries the fix.

### Obligations per group, and what a guard can assert
- **Carriers (16) and inheritors (14)** share one *reader* invariant: a query over an inheritor is safe only when whoever
  resolved its parent did so **under the scope predicate**. That is a property of code, not data, so a DB test over
  these rows asserts **nothing** — there is no second column to compare.
- **The enforcement is not a sweep over the SQL text**: measured (app, 2026-10-17), such a sweep flags 23 of 23 query
  blocks and every flag is correct code, because the **caller** resolves the parent in scope and passes its id down, so
  the child query has no parent join by design (`../crm-cdp-app/services/api/routers/analytics_explain.py:54-64` +
  `../crm-cdp-app/db/queries/analytics_explain.sql:19-25`). The checkable invariant is cross-file: for every child-query
  call carrying a parent id, the same code path must first scope-guard that parent. That guard is **not built**.
- **Carriers with a second artifact parent — 2 edges, not the same case.** `explain_batches → segment_runs` (**writer:
  ml**): equality is structural (the child's stamp is copied from the parent row read in the same call) — live 2026-10-17
  **7 compared / 0 violations**, the paired run-identity check (`explain_batches.fit_id → seg_surrogate_fits`) **2 / 0**.
  `pipeline_runs → connector_sync_runs` (**writer: data, both sides**) is **not** one statement: the sync row takes the
  session's scope (`services/api/repos/connectors.py:218-224`), the run row's stamps come as caller flags
  (`scripts/run_pipeline.py:113-116`) stored by `pipelines/schedule.py:75-89`, and **the runner never reads the sync
  row's stamps**. Live **0 rows** — untested, not verified; the harness is data's, because the writer is.
- **Carriers that also inherit — the row's own stamp governs.** `explain_batches.segment_run_id uuid NOT NULL REFERENCES
  segment_runs (id)` (`021_explain.sql:18`) sits alongside its own `brand_id`/`group_id` (`:29-30`);
  `pipeline_runs.sync_run_id` (`023_pipeline_sync_link.sql:12-13`, NULL) is the same shape. **The row's own stamp
  governs** (app ruling 2026-10-17): it is the only column `row_visible` reads, and the parent FK is lineage, not a
  second visibility path. A child/parent stamp disagreement is **a write bug**, not a case to define away: guard it at
  the writer that inserts both in one transaction (`ml_engine/store_explain.py:239 explain_run`).
- **Negatives (7)** have no visibility obligation (an ordinary population filter); unscoped shape 1 gates at the reader
  and abstains at the writer, shape 2 has neither. **Expressible is not enforceable**: seeds with NULL scope at both ends
  cannot fail a stamp-equality assertion — teeth need writer-driving tests (ml's `store_explain` / `store_ld8` /
  `store_rfm`, data's `test_schedule.py::test_sync_lineage_stamps_agree`).

**The trap in any schema-derived edge list.** Enumerating FKs over scope-carrying tables yields **25 edges with a stamp
at both ends, 23 of them into dimension relations** (`brand_id → brands.id` ×20, three into `customers`/`stores`).
Exclude them: a brand-scoped row carries `group_id = NULL` while its `brands` row carries the owning group, so equality
there is **wrong by design**. Only the **both-stamped** artifact→artifact edges (the 2 above) count.

**A third artifact→artifact edge, whose invariant is not a stamp.** `explain_batches.fit_id → seg_surrogate_fits.id`
(`030_surrogate_fits.sql:29-30`, `uuid NULL … ON DELETE SET NULL`) is not between two stamped ends —
`seg_surrogate_fits` (`:15-24`) carries `segment_run_id` and no scope column — so assert **run identity** instead
(`explain_batches.segment_run_id IS DISTINCT FROM seg_surrogate_fits.segment_run_id` must be empty), not a stamp.

## Tree ownership & the mirror boundary (app ruling 2026-10-17)

- **The rule:** for any file in **both** trees app's checkout is the base — copy it byte-for-byte, **never fork it**,
  **never delete a data-only file**. Data holds a **bootstrap subset**, not a mirror (the data-only `test_apply.py`,
  `test_upload.py`, `test_schedule.py`, `db/queries/kpi_daily.sql`, … are harness, not drift); `services/api/tests/**`
  is data's own, so a sync copies the runtime and data updates **its** tests when app's runtime changes shape.
- **Closure — the unit of copying is the import closure, not the module.** Mirror a module with the files it reads *at
  import*, or the tree imports and cannot boot: app's `services/api/analytics/periods.py` reads
  `db/queries/analytics_comparisons.sql` at module level, so copying `routers/stats.py` + `repos/stats.py` without both
  left the API unable to start (`FileNotFoundError` in every client fixture — 40 errors, none about stats). Same shape
  where `services/api/analytics/snapshots_serve.py:48-54` imports `ml_engine` at module level: keep any spec-file read
  **opt-in via an explicit argument, never a module-level constant** (ml's default is no read —
  `ml_engine/features.py:286-287`, `DEFAULT_ENABLED` when `path is None`).
- **A moved writer's layer is not a fork:** `note_sync_result` (data's logic in data's copy of the app-owned
  `services/api/repos/connectors.py`) now lives in the data-owned `pipelines/ingest/connector_sync.py`.
- **Compare blobs, not worktrees** (`core.autocrlf=true` both sides): after the 2026-10-17 sync the shared paths showed
  **20 worktree mismatches (16 CRLF-only, 4 already-excluded harness files) against 25 of 25 byte-identical blobs**
  (`git cat-file blob <rev>:<path>`), so a worktree diff on a clean mirror is a false alarm.
- **No cross-tree file reads at runtime**: each tree boots from its own copies and what crosses the boundary is declared,
  not an ad-hoc path read — app's `pip install -e ../crm-cdp-ml` (`ml_engine/__init__.py:10`) and vendored
  `pipelines/ingest/connectors.py`; data executing ml's checkout as a subprocess on every ml job
  (`pipelines/schedule.py:13`, `:841`/`:846`); ml's SQL depending on the schema these migrations create.

## Pin half-life, and the rule it settled (2026-10-17)

**A revision label records a read; a bump is a claim of re-verification.** A label (`read at ml <sha>`) survives any later
edit to the file it names; a pin's half-life is the next commit to that file, so a pin's only promise is that the delta
gets printed, never that the value is fresh — and a bump asserts a re-reading: never bump to clear a red.
