# Source → table contracts

Reading citations here: the **symbol** is the anchor (`store_ld8.run_ld8`,
`_checked_body`, `PAIR_WINDOWS`) and a line number is only ever stated with
an `as of <commit>` — a bare line number is a promise the next commit breaks
silently. Grep the symbol; refresh the anchors when you touch the cited file.
**Pin the revision and both ends of the span together**: cite what you
actually read, at the revision you actually read it (`git show <sha>:path`),
and end the citation where the *statement* ends rather than where your
reading did. When two readings differ by a **constant offset**, suspect two
revisions rather than an error: one line added above a span shifts every
number below it (`e423f70`'s third `RETURNING` line moved the INSERT's
parameter block from `:178` to `:179`, and `engine.RULES_VERSION` from `:152`
to `:153`), so `git show` both revisions before publishing a correction. A
peer's worktree is a moving target — a reviewer reading it
while the peer lands the fix will confirm the fixed state and can call a
correct claim wrong, which puts a false correction in this file rather than
in a chat (both happened here on 2026-10-17: the `store_rfm` `RETURNING`
attribution, and two half-statement spans). Pin line numbers **once per cited file, per
revision** (one `as of ml <sha>` line for a whole section) instead of per
claim, and cite the revision **you read**, not HEAD — a witness number stays
true forever, while "as of HEAD" rots on the next unrelated commit to that
file. In a **code comment** (which carries no revision context at all) cite
the content plus that pinned sha, because its reader is the person least
likely to know the thread existed (app, 2026-10-17).

Anchors carry their **file's** last-changed revision, not ml's repo HEAD
(`git log -1 --format=%h -- <path>`), because a HEAD pin rots on any commit
to *any* file (ml, 2026-10-17) — and per-file pins are only half the rule: a
commit **inside** the pinned file still moves every anchor below it, so pins
are taken per file and anchors re-derived per cite, never offset (app,
2026-10-17). As read here: `ml_engine/store_rfm.py` `e423f70`,
`ml_engine/store_ld8.py` `9f9093f`, `ml_engine/rfm.py` `4a8f994`,
`ml_engine/store_segments.py`/`store_explain.py`/`store_forecast.py`
`4a8f994`, `ml_engine/segment_movement.py` `e8934a7`, `ml_engine/btyd.py`
`87734e8`, `ml_jobs/manifest.py` `0b834f9`, `ml_jobs/run_matrix_batch.py`
`bb6b45c`.

Two further failure modes need no movement at all, and archaeology separated
them (app, 2026-10-17, times +0800 from the commit clock):

- **Stale at birth** — construct right, number right *in the message it was
  copied from*, and already out of date when written. app's
  `db/queries/analytics_segment_matrix.sql:3` cited `model_fits.py:54` at
  21:13:54, but `:54` stopped being `MATRIX_FOR_RUN_SQL` at data `294517b`
  (18:15:16) — three hours earlier. One sentence, two sources seven minutes
  apart, two revisions; no "rotted by <commit>" sentence can describe it.
  This is the **dominant** failure here: **copy-lag, not rot**.
- **Misaimed construct** — the number never moved and was never right. It has
  **no verified instance** in this thread; both candidates collapsed once the
  *write* revisions were checked. ml's `store_ld8.py:70` cite was correct when
  written (data's `snapshot.py:342` was `SEGMENT_SIZE_UPSERT_SQL` from
  `412f9d7` 18:13:32 through `5f91e16` — the four mirrored statements were a
  contiguous run seven lines apart, `:342`/`:349`/`:356`/`:363`, so the cite
  named the **first line of the exact block it described**, and they are
  `:373`/`:380`/`:387`/`:394` (+31) at `f889386`) and rotted only with data's
  `f889386` (10-17 01:09:24); app's `db/queries/rfm_windows.sql:2` is the same
  case — at its 22:06:05 write `:342` *was* the row-defining statement, so the
  defect was an **ambiguous referent** ("canonical row SQL", writer vs
  readers), not a wrong construct. Keep the check, not the examples.

So "was it right when written?" is a question about **write timestamps across
repos**, not about the revision the author says they read, and the pin has to
be taken **at paste time**. What stands demonstrated is the part that makes
any of this decidable at all: each sentence above was unresolvable while it
was a prose referent plus a number — naming the construct is the fix, not the
number. A bare `:NN` is legitimate where the file is named in the same
sentence (both repos cite that way; app's sweep found six such hits and no
defect), but a bare number with **no referent in scope** clears all three
checks at once because there is no file to disagree with — data's own
`:938-943`, offered as the file's `segment_movement` pair cite, matched no ml
file at all and is the one instance of that class found here (data,
2026-10-17). Two copies of one source line also share one **expiry date**: ml's
`store_ld8.py:70` (21:29:02) and app's `db/queries/rfm_windows.sql:2`
(22:06:05) both cited `snapshot.py:342`, both were true when written, and
both died in data's `f889386` window — so "I verified this citation" is not
portable, only "verified for the tree it was written in".

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
Daily KPI job: `python scripts/run_daily_snapshot.py --as-of YYYY-MM-DD`
— it writes **one `kpi_daily_snapshots` row and nothing else**. The ML
`run_snapshot` / `run_recs_batch` lines in its docstring are sibling
invocations to run separately (then log to `model_training_runs`); the
script never writes `rfm_snapshots`. Model runs are scheduler manifest
jobs — see **Daily RFM writer** below. Queries in
`db/queries/kpi_daily.sql`.
Scope columns (`brand_id`, `group_id`, NULL = group-wide) match 012.

**Day boundary is business time (Asia/Hong_Kong, +08) — rule added
2026-10-17.** `as_of` / `window_from` / `window_to` are business-day
dates, not server- or host-timezone days: the effective instant of a
bound named `2026-10-16` is `2026-10-16T00:00+08` = `2026-10-15T16:00Z`.
`rfm_snapshots.window_from` is a `date` column, so **no stored row
reveals the offset** — a reader who assumes `SHOW TimeZone` (GMT on this
stack) or the host clock silently disagrees by up to 8h. ML now pins the
naive->aware conversion explicitly (`ml_engine/rfm.py:_day_start`,
`ZoneInfo("Asia/Hong_Kong")`); zero stored values change on a +08 host,
so no `RULES_VERSION` bump was needed.

Two data-side gaps in the same class (found 2026-10-17, owner GO
2026-10-17, **both FIXED in the same commit** — see below):

1. `pipelines/snapshot.py` cohort SQL truncated with
   `date_trunc($1::text, ordered_at)`, which PG evaluates in the
   **session** zone (GMT) — so week/month cohort buckets were UTC
   boundaries while the snapshot dates are +08. Blast radius over 8,685
   orders: 137 shift month bucket, 628 shift week bucket, 3,698 shift
   day bucket.
2. The `current` cutoff was server `CURRENT_DATE - 1`, while
   `scripts/run_daily_snapshot.py` / `scripts/run_pipeline.py` defaulted
   `--as-of` to host `date.today()`; during 00:00-08:00 local those
   disagree (observed server `2026-10-16` vs +08 `2026-10-17`).

**FIXED 2026-10-17 (`pipelines/snapshot.py`, no migration, no schema
change):** every bucket-deciding trunc now carries the offset
(`BUSINESS_TZ_SQL = f"AT TIME ZONE interval
'{BUSINESS_TZ_OFFSET_HOURS} hours'"`), the cohort UPSERT + `/stats/overview`
month series use it, and the cutoff source is explicit:
`business_today()` = `datetime.now(timezone(timedelta(hours=8))).date()`,
used by both scripts as the default `--as-of`. The zone NAME is kept in
`BUSINESS_TZ_NAME` + the constant's comment so the offset never floats
free of the business zone (app pins `TIME_ZONE` in
`apps/web/lib/format.ts`, ML pins `ZoneInfo("Asia/Hong_Kong")` — all
three repos now name the same business day).

**Cohort figures change by design and old/new are NOT comparable.**
Recomputed over the 940 purchasing customers: 10 customers' month
cohort, 62 week cohort, 0 year cohort move (they are the customers whose
FIRST order sits in the 16h band). `cohort_snapshots` was purged and
regenerated in the same commit — the grain UNIQUE
(`granularity, cohort_period, period_offset, COALESCE(brand_id),
COALESCE(group_id)`, `020_cohort_snapshots.sql:31`) has **no tz
component**, so without the purge a re-run would leave the UTC
generation alongside the +08 rows and the heatmap would silently mix two
generations. Verified post-fix: stored offset-0 `cohort_size` ==
+08 recomputation for all 36 month / 152 week / 4 year buckets, 0
missing, 0 extra, 0 mismatches; 940 members at every granularity.

Environment note: this pgembed install ships **no tzdata**
(`pgembed/pginstall/share/postgresql/timezone` missing), so no named zone
works in SQL here — `AT TIME ZONE 'Asia/Hong_Kong'` raises
`time zone "Asia/Hong_Kong" not recognized`, while an explicit offset
(`AT TIME ZONE interval '8 hours'`) works. Any SQL-side pin must use the
offset form (Hong Kong has no DST, so it is semantically identical
forever).

**Which `as_of` default do you get? (inventory, 2026-10-17, after ml
`4a8f994`).** Read the call site, not the column — and since `4a8f994`
there is exactly ONE default in the ml package:

| source (default when `as_of` is omitted) | resolves to | business-day? |
|---|---|---|
| data `pipelines/snapshot.py:245 business_today()` (`+08` fixed offset), used as the default `--as-of` by `scripts/run_daily_snapshot.py:70` and `scripts/run_pipeline.py:144` | +08 business date | yes |
| data scheduler — `pipelines/schedule.py:293` in `_with_common` | passes `--as-of ctx.as_of` for every engine whose CLI declares it; `explain_batch` / `assoc_batch` / `matrix_batch` / `recs_batch` take no `--as-of` at all (argparse rejects it — `pipelines/schedule.py:441`), so their args are always explicit or absent, never guessed | n/a (explicit) |
| ml `ml_engine/rfm.py:79 business_today()` (`ZoneInfo("Asia/Hong_Kong")`) — the default applied by `ml_engine/rfm.py:123` inside `resolve_window(...)` (`:101` def), `ml_engine/store_ld8.py:240 run_ld8(...)`, and the `_coerce_as_of` of all four other run writers (`store_churn.py:217`, `store_clv.py:189`, `store_forecast.py:50`, `store_segments.py:419` — each imports the helper directly, so no UTC path remains) | +08 business date | yes |

Closed by ml `4a8f994` (no migration, no schema, no `RULES_VERSION`, no
argv change; guard `tests/test_engine.py:382-408` pins an in-band instant
17:37Z = 01:37 HKT and asserts all four plus the explicit
str/date pass-throughs).

History, because the first version of this note **overstated** it: the
four copies read the UTC clock, but only **segments** contradicted
itself. churn/clv anchor their calibration frame at a UTC *instant*
(`store_churn.py:147`, `store_clv.py:111`) and forecast derives its
periods from the same `as_of_date` — and no date from that frame is
persisted (`churn_snapshots`' insert takes only `as_of, activity,
horizon_days, fit, customer_count, created_by, brand_id, group_id`;
`ml_engine/btyd.py:359 summarize_fit` returns no date), so those three
were consistently UTC: wrong convention, not self-contradicting.
Segments was the one whose UTC *label* disagreed with the business-day
window `resolve_window` had fitted.

**`--as-of` labels a run; it does not anchor a fit (ml, 2026-10-17).**
Now stated in the code rather than inferred: the OVERALL/current segments
fit is **always the current rolling-365 business window** (`_fit_overall`,
docstring at `ml_engine/store_segments.py:174-181`; same comment at
`store_explain.py:97-98`), so a run row's `as_of` is an
idempotency/version key, not a window key, and `compute_current` /
`overall_segments` take no `as_of` at all. Threading one in would be a
historical-re-fit feature (a second source of truth for the same fit),
deliberately not done. Operationally, for a backfill: **only `forecast`
(`as_of` → `forecast_demand`, `store_forecast.py:80-81`) and `ld8`
(`resolve_ld8_window(key, as_of)`) derive a window from `--as-of`** —
churn/clv fit against `datetime.now(timezone.utc)` and segments-overall
explains fit the current window, so re-running an old `as_of` there
*relabels* today's fit instead of reproducing an old one.

**Name collision — `resolve_window` is two different functions.** data
`pipelines/schedule.py:130 resolve_window(mode, n, unit, ...)` resolves a
**cohort/period** window; ml `ml_engine/rfm.py:96 resolve_window(mode,
as_of, date_from, date_to, *, max_span_days=731)` resolves the **RFM
snapshot** window (tz-aware `+08` day boundaries; `max_span_days=None` is
the caller asserting a deliberate full-history span, not a clamp). A bare
grep returns both — check the repo before reading either as the other's
contract.

**Server-zone evaluation in data-owned SQL: one CHECK, nothing else.**
Every stored date in the pipeline is passed as an explicit `$1::date`
parameter, so the postmaster's zone never decides a stored value. The
single exception is `db/migrations/017_customers_demographics.sql:11`
`CHECK (date_of_birth IS NULL OR date_of_birth <= CURRENT_DATE)`, which
PG evaluates in the **server** zone (GMT on this stack). During the same
00:00-08:00 HKT band that makes `date_of_birth` = today a constraint
violation (server `2026-10-16` while the business day is `2026-10-17`).
Low impact — DOB = today is implausible outside fixtures — and
deliberately not "fixed": the honest repair is a *looser* bound or
read-time validation, never a stricter one, because a false rejection of
a real newborn's DOB is worse than accepting a one-day-future value.
Tracked in SCRATCHPAD.

## Daily RFM writer: `ld8_snapshot` (v2) vs `rfm_snapshot` (v1)

`rfm_snapshots` has two *scheduled* writers with different rulesets, and
the engine name in the manifest decides which one a tick uses (a third,
non-scheduled writer — the app's snapshot POST — is at the end of this
section):

| engine | ml job -> module | rules | windows | conflict behaviour |
|---|---|---|---|---|
| `rfm_snapshot` | `ml_jobs.run_snapshot` -> `ml_engine/store_rfm.py` | **v1** (`ml_engine/rfm.py:24 RULES_VERSION = "v1"`) | one, from `params.mode` | **APPEND-only**: `SNAPSHOT_INSERT_SQL` has no `ON CONFLICT` and no find |
| `ld8_snapshot` | `ml_jobs.run_ld8_snapshot` -> `ml_engine/store_ld8.py` | **v2** (`store_ld8.py:36 RULES_V2`) | FOUR: `current` (rolling 365d ending as-of) \| `mom` \| `yoy` \| `lifetime` | find-or-create on (mode, window_from, window_to, window_key, scope), filtered `rules_version='v2'` |

031 declares v2 for time-aware rows, so **v2 is canonical for
`window_key='current'`** and `ld8_snapshot` is the engine the daily
capture belongs on. `rfm_snapshot` stays available for v1 history /
backfill only.

**The trap this closes (proved on live, 2026-10-17):** with the daily job
on `rfm_snapshot`, the v1 insert is append-only and the v2 find is
v2-scoped, so one v1 and one v2 row land for the same
`window_key='current'` window **every day** — the newest two are
cross-version by construction, not by accident.
`GET /analytics/rfm/movement?mode=rolling_365` auto-selects the two most
recent rows by `window_to` and only then rejects a version mismatch, so
the default path answers **422 `snapshot_mode_mismatch`**.

Switching the job is a manifest edit, and **`pipeline_manifests` rows are
edited through the app config API, never by migration** (015 seed
comment):

    rfm:
      engine: ld8_snapshot
      depends_on: [apply]
      params: {}          # drop `mode: rolling_365`: the LD8 CLI has no --mode

The builder rejects the legacy param with a named `ManifestError` rather
than letting the CLI exit 2 on every tick (`pipelines/schedule.py`
`_build_ld8`).

First-tick triage, so a failure is not misread as the wrong subsystem:
the argv's positional DB URL is the runner's resolved `ctx.db_url`
(`pipelines/schedule.py:281` emits `[python, -m, <module>, ctx.db_url]`,
sourced from `scripts/run_pipeline.py:96-116` via `_connect_resolved`),
**never** a manifest `params` value. So a wrong row fails at dispatch as
a `ManifestError` with a message *above* the spawn, while a bad URL is a
runner/env fault that takes every ml job in the tick down with it — and
an unset one is caught pre-spawn too (`pipelines/schedule.py:278-280`,
`UnknownEngineError`). The ml CLI connects *before* it validates its own
args (`ml_jobs/run_ld8_snapshot.py:20-27`), so a malformed `--as-of` or
scope UUID surfaces as `ValueError` only after a successful connect and a
bad URL as a driver error (`exit 1`, e.g. `socket.gaierror` /
`ConnectionRefusedError`) — which reads as a connectivity incident. That
ordering only bites **hand-runs**: the tick always passes
`ctx.as_of.isoformat()` (`pipelines/schedule.py:293`, a `date`, so never
malformed) together with the runner's own URL.

`--as-of` is also a **silent no-op knob on a window that already
exists**, and on the tick it is always explicit (`_build_ld8` →
`_with_common`, `pipelines/schedule.py:340`/`:286-293`; the ml CLI's own
`business_today()` default is therefore never the tick's — and it is not
hand-run-only either: the app's own
`POST /analytics/{churn,clv,segments}/snapshots` calls `store_*.create_snapshot`
**in-process with `as_of` omitted** (app `services/api/analytics/snapshots_serve.py:48-54`,
imported at request time), so an `ml_engine/` change is live for `ml_jobs/`
immediately (fresh process per run) but **live in the API only after that process
restarts** — "landed" and "live" differ by one process lifetime on the serving
side; the test is to compare the changed file's mtime against the serving
process's start time rather than infer it from `git log` (app runbook
`docs/reseed-runbook.md`, `9674976`: `Get-NetTCPConnection -LocalPort 8000
-State Listen` for the PID, then `Get-Process -Id <pid> | Select StartTime`)).
`ld8_snapshot` find-or-creates on `(mode, window_from, window_to, v2,
window_key, scope)` (`ml_engine/store_ld8.py:63-66`, the `SNAPSHOT_FIND_V2_SQL`
find predicate), so an `--as-of`
whose `current` window is already stored — e.g. `--as-of 2026-10-16`
while the v2 row for `2025-10-16 → 2026-10-16` exists — exits 0 having
written nothing. The tick is green, no row is wrong, and a consumer that
needs a *newer* period (movement needs two periods of one version) stays
on its `need_two_snapshots` 404 with nothing anywhere to point at. Rule
for a catch-up or `--force` tick: omit `--as-of` unless you intend a new
period, and never reuse a date whose window is on disk.

`created_by` is passed through (`--created-by`;
`scripts/run_pipeline.py:132` default `scheduler`), so through the runner a
NULL is **unreachable**. Every `ml_jobs/run_*.py` that writes a
`created_by` column declares `--created-by` with `default=None` (10 of the
12; `run_alive_path.py` and `run_matrix_batch.py` write no such column), so
a NULL in the column dates the row to a **direct ml-CLI hand-run** rather
than to a caller who forgot the flag (`rfm_snapshots.e234dc57` is one). Pass a staff code wherever a job writes,
or the row is indistinguishable from "written by an unset caller" (this
cost a manual row dump to diagnose).

The hole is **structural, not incidental**: all 12 `created_by` columns in
this repo's DDL are declared `text NULL` with no `DEFAULT`
(`db/migrations/007_customer_events.sql:14`, `010_analytics_rfm.sql:15`,
`011_recommendations.sql:14`, `013_analytics_history.sql:24,57,115`,
`014_upload_staging.sql:39`, `015_segments_pipelines.sql:37`,
`018_assoc_rules.sql:25`, `019_connectors.sql:27`, `021_explain.sql:27`,
`022_forecast.sql:30`), so an unstamped hand-run **cannot fail anywhere** —
there is no NOT NULL guard that would have turned an unattributed write into
an error. Data's own CLI follows the same convention (`scripts/run_upload.py:124`
declares `--created-by` with `default=None`; the API path stamps the session
instead, `services/api/routers/customer_events.py:73`). Making `--created-by`
**required** in the 10 ml CLIs was considered and **declined, not missed**:
it is a 10-file argv change (`[BREAKING @both]`) and it forces every
dev/one-off invocation to pass a value, so the rule below closes the hole
operationally instead.

Never pair v1 with v2 for movement: different writers decide the windows
(same maths both ways — `store_ld8.compute_window` calls
`store_rfm.compute_current` verbatim, `ml_engine/store_ld8.py:141-175`;
v1 is an append-only single-window write, v2 is a four-window
find-or-create, and the *engine dict* decides the version, not the caller
(and not the writer): the v1 INSERT takes it as a parameter
(`store_rfm.py:36`, `:179` passing `computed["rules_version"]`, which
`compute_current` set from `engine.RULES_VERSION` at `:153` — `"v1"` at
`rfm.py:24` — so `create_snapshot` never reads the constant), while the v2 INSERT
interpolates the literal `'{RULES_V2}'` (`store_ld8.py:56` in the
INSERT's VALUES, `:64` in the find's WHERE, `RULES_V2 = "v2"` at `:36`)), so cross-version movement is meaningless. The serving-side fix is
to take the newest snapshot's `rules_version` and then the two newest **of
that version** for that `mode`/`window_key` in scope — returning the existing
`need_two_snapshots` 404 when only one exists — not to relax the mismatch
check. Note the pair is the two newest rows of the same `mode` **and the
same `window_key`** — the *bounds* need not match and normally do not
(the live pair today is `92537b23` (`2025-10-17 → 2026-10-17`,
`created_by=scheduler`) with `e234dc57` (`2025-10-16 → 2026-10-16`,
`created_by=NULL`), both v2 and both `window_key='current'`, and
`GET /rfm/movement?mode=rolling_365` answers 200 off them (app readback
2026-10-17). Differing bounds are the normal case, since each tick
find-or-creates its own window — which is also why a same-day re-run adds
nothing to the pair. The `window_key` separates windows *within* a mode
(`current` vs `mom`/`yoy`/`lifetime`); it never pairs across them, so a
two-row requirement is always **two nights of the same window**, never
two different keys (this sentence read "two different `window_key` values for
the same `mode`" until ml corrected it, 2026-10-17 — the error was data's
prose, never the schema).
Note `SNAPSHOTS_LIST_SQL` (`store_rfm.py:53-66`; SELECT list `:54-55`,
ordering `:64`) **did not project `window_key` until ml `e234563`**
(2026-10-17, both snapshot reads — this one at `:54` and the by-id read), so
before that a list client could not see which window a row belonged to; it
orders `window_to DESC, scored_at DESC, id DESC` (period, not compute time).
Pairing is **version-inclusive** — same `mode` + same `rules_version` +
same `window_key` — and **which term is load-bearing depends on the mode**:
the version term carries `rolling_365`, where a v1 `current` row sits beside
v2 `current` rows, while in `custom` it is vacuous (that mode holds no v1 row
at all) and a cross-*window* pair is what the key term prevents. The version
term has to carry `rolling_365` because
`window_key` **cannot** separate v1 from v2: it is `NOT NULL DEFAULT
'current'` (`db/migrations/031_rfm_time_aware.sql:19-20`), so the v1 INSERT
omitting the column (`store_rfm.py:35-43` — nine columns at `:37-38`, and
it is not one
of them; the v2 INSERT at `store_ld8.py:52-59` sets it as `$9`) means the
**DDL default fills it**, and a v1 `rolling_365` row carries `'current'`
exactly like a v2 one. Verified live 2026-10-17: `information_schema` says
`is_nullable=NO, default='current'::text`, 7 of 7 rows non-null, and the lone
v1 row (`57414845`) reads `'current'`. That kills the nullability reading of
this rule that data recorded in `443ba03` (withdrawn: *the statement not
writing a column is not the row being NULL*). App-visible consequence of that
projection gap: the app consumes this list
query **directly** (`../crm-cdp-app/services/api/routers/analytics_rfm.py:37`
`from ml_engine import store_rfm as repo`), and its `custom` tab pairs the two
newest `custom` rows whatever window they held. Data's first reading of that
was wrong and app corrected it with a live inventory (2026-10-17): mode
`custom` holds **four v2 rows and no v1 row at all** — `lifetime` `807d4f38`
(`…→2026-10-17`) and `2895ba93` (`…→2026-10-16`), `mom` `880c0cbe`, `yoy`
`8950aaec` — so the pair the page actually takes is **same version and same
`window_key`**, differing only in `window_to`, which the predicate above
accepts correctly; it is not a version or key mispairing. What is wrong there
is **label vs window**: the band prints run dates,
`formatDate(movement.from.scored_at) → formatDate(movement.to.scored_at)`
(`apps/web/app/rfm/page.tsx:501`), while the rows compared are all-time
cumulative windows. Root cause worth keeping: `mode='custom'` in
`rfm_snapshots` is **LD8's named-window bucket** (`mom`/`yoy`/`lifetime`), not
the user's custom date range — the tab's *scores* come from a live computation
over the requested dates, its *band* from that bucket, and `rolling_365` reads
correctly only because there the stored `current` window is what the mode
means. So the missing projection is an identification gap, not a pairing bug;
the tab's fix is the period rule below, and under either shape today's visible
outcome there is the empty state (app/ml, 2026-10-17).

Chosen shape for that tab (app, 2026-10-17): the page's period rule goes
window-shaped — pin `current` where the mode defines the window, requested
bounds on the custom tab — **plus** the guard's key term and a bounds label on
the band. Showing the stored `lifetime` pair labelled with its real bounds was
rejected, because the page's own empty-state copy already promises the band is
about *this window* ("appears once two snapshots of this window exist for this
scope"), so it would be truthful yet contradict the surface it sits on; an
all-time or standalone movement view is where that shape belongs. All three
parts are app-side only — no ML edit, no migration, no extra request, since
`_snapshot_row` (`store_rfm.py:86-97`, isoformatting the bounds at `:92-94`
and `scored_at` at `:95-96`) is already in the payload the page holds via
`movement()`'s `"from"`/`"to"` dicts (`:297-299`) — and the guard term alone
changes nothing visible, because the pair it currently compares shares a key.
Two things are still the owner's call rather than either team's (app,
2026-10-17): the shape itself, and the **empty-state copy**, which under this
shape is mode-shaped — "appears once two snapshots of this window exist for
this scope" implies an achievable wait that is true for `rolling_365` and
year-to-date and false for a custom range, where option (a) makes the stored
band *unreachable* rather than pending until the POST path is wired. The
trade-off in one line each, for the owner (app, 2026-10-17): **(a)** state
plainly that the band cannot be computed for a range — a copy change in both
locales; **(b)** show the all-time pair with its real bounds — no copy change.
App recommends (a); nothing else differs between them.

**Successive-period rule, branched by what the page displays** (app/ml,
2026-10-17, both branches verified live against the pairs above). Where the
page shows a *mode-defined* window (`rolling_365`, `year_to_date`), successive
periods are equal-span windows shifted by the run cadence, so the test is
**translation** — equal deltas at both ends, equal to the cadence: the
`rolling_365` pair gives 1d/1d and is accepted, while the `custom` `lifetime`
pair (`window_from` pinned to the 2023-11-12 floor) gives 0d/1d and is
rejected, correctly, because that window *grows* rather than shifts. Where the
page shows an *explicit range* (the custom tab's `date_from`/`date_to`),
successive periods are two runs of **that same range** — identical bounds,
different `scored_at` — so the test is **equality with the request**, not
translation; translation alone accepts any two same-length ranges in different
months (`2026-09-01→09-30` vs `2026-08-01→08-30` → 30d/30d → accept, and vs
`2026-10-01→10-30` → accept), which is what a date-range tab produces on its
second use. The `window_key='current'` pin stays in both branches as the cheap
sanity check: `custom` holds no `current` row at all — LD8's reachable space is
`rolling_365×current`, `custom×lifetime`, `custom×mom`, `custom×yoy`, with
`custom×current` unreachable from the writer (`ml_engine/store_ld8.py:154-155` four-combination statement,
mapping applied at `:174`) — so the pin yields the empty state structurally rather
than by accident.

The POST path is why the explicit-range branch exists rather than being
deferred: `SNAPSHOT_INSERT_SQL` (`store_rfm.py:35-43`) omits `window_key` from
its column list (`:36-38`, nine columns), so the DDL default stamps `'current'`
on every app-POSTed row **regardless of mode or user dates**. The `RETURNING`
did not name the key either until ml `e423f70` (2026-10-17, *fix(store_rfm):
name the window in the write path's RETURNING too*), which makes this the
**third** snapshot projection to carry it (`:40-42`; `git show
e234563:ml_engine/store_rfm.py` shows the pre-fix `RETURNING id, mode,
window_from, window_to, scored_at, …`), and ml's
`test_snapshot_projections_expose_window_key` now covers all three. So a
POSTed row comes back **self-identifying** — `mode='custom'` *and*
`window_key='current'` — in a response the app already holds, with no schema
change and no extra request. Two
app-POSTed `custom` snapshots for different user ranges would therefore both be
`current` and pair under a `current` pin alone (app/ml, 2026-10-17).
`window_key` does have a job in this area — in the **guard**, not in the
predicate: the movement guard checks `mode` + `rules_version` only
(`analytics_rfm.py:265-270`), so a cross-key pair is accepted with 200, proven
live `from=880c0cbe` (`mom`) → `to=807d4f38` (`lifetime`) → `customers_both
252, entered 688, exited 0`, which is the window widening, not movement. Scope
that fix to this endpoint: LD8's own `segment_movement` pairs different keys
**by design** (`mom`→`current`; `ml_engine/segment_movement.py:23-26`
`PAIR_WINDOWS`), so same-key is a property of the
app's run-to-run diff, not of the concept (app, 2026-10-17).

Those two INSERTs are the only production writes of `rules_version`
(`store_rfm.py:36`, `store_ld8.py:53`; the app's POST reuses the former,
and its only direct INSERTs are in
`../crm-cdp-app/services/api/tests/test_rfm.py:451/513/537`), so a scope
holding **only v1 rows** is exactly a scope **LD8 has never been ticked
in** — the check is `SELECT DISTINCT rules_version` for that scope, and
the remedy is a scoped LD8 run, not an empty-state fix (ml, 2026-10-17).
The copies are now guarded on the **copying** side: ml `ef4c759` adds
`tests/test_ld8.py::test_031_statements_still_match_data_runner`, which reads
this repo's `pipelines/snapshot.py` (via `DATA_REPO_PATH`, default
`../crm-cdp-data`), extracts the four statements by name and asserts
byte-equality (modulo `_norm`), skipping when the repo is absent — the same
convention app has offered the owner for its `db/queries/*.sql` copies.
(`test_031_sql_verbatim` in the same file is the inline-literal guard, which
catches an edit on ml's side and cannot see one here.) One **data-side
consequence**: those four statement *names* in `pipelines/snapshot.py` are
now effectively frozen — the `skipif` is keyed on `SEGMENT_SIZE_UPSERT_SQL`
alone, and any other name's disappearance fails ml's suite with
`"{name} missing from data's snapshot.py"`. Data remains the source of both
copy sets, so nothing mirrors here; what data can offer — versioning
`pipelines/snapshot.py`'s four LD8 constants and the `031` schema as a frozen
interface — is the owner's call, not a unilateral one. **Two consumers, and
the freeze and the guard are complements** (app, 2026-10-17): app's four
`db/queries/*.sql` copies lag data's edits exactly as ml's constants do, and
fail *worse* — self-contained SQL keeps serving an old shape with no error
anywhere, where a lagged ml constant at least raises a column/conflict error
— so the freeze would have to cover ML's four `snapshot.py` statements **and**
app's four copies, with the `031` schema under both. A guard alone reports
every lag after it happens; a freeze with no guard still lets a later
re-freeze lag unnoticed. For app the freeze is the **cheap** half, because
nothing in the app reads data's tree at serve time: those four copies
*already are* the frozen artifact, implicitly and with no label saying which
revision each was taken from, so what the freeze adds there is a **version
marker** rather than a new artifact — and the guard stays a developer-time
detector on that side too (app, 2026-10-17).

**`m_cap_pct` is a live-request knob, never a stored property.** The cap
is an engine kwarg (`ml_engine/rfm.py:278` → `store_rfm.py:115/140`,
`store_ld8.py:157-174`) reachable from the app's two live GETs only
(`../crm-cdp-app/services/api/routers/analytics_rfm.py`, the `m_cap_pct`
query params `:115`/`:279`, `Query(ge=0.01, le=1.0)`, passed straight to the
engine at `:125`/`:290` — as of app `2285bfd`; symbol names are the stable
anchors, these line numbers move); the scheduled writer always uses
`DEFAULT_M_CAP_PCT = 0.99`, and no renderer emits a flag for it, so the
`m_cap_pct: (False, None)` entry `rfm_snapshot` carried was a declaration
nothing could turn into argv (removed, ml `f78d127`). Deleting it is
editorial, not protective: `_validate_job` (`ml_jobs/manifest.py:226-234`)
checks known keys only for `required`/`allowed` values, and unknown params
pass through by design (`:64-72`, so that scheduler-only keys reach the
pipeline), so a stored row setting it would still validate and still be
ignored. The guards are this line, the absent renderer, and app's pin. The snapshot
write path is **pinned by decision** (app `6bdf2b4`; guard `_checked_body` at
`analytics_rfm.py:55-67` as of app `2285bfd`, applied on the POST at `:140`):
the body default is the engine constant (`:52`) and `_checked_body` rejects a
non-default cap by name — `422 {"code": "non_default_m_cap_pct",
"default": engine.DEFAULT_M_CAP_PCT}` (`:61-64`) — and any cap outside
`(0, 1]` with `bad_m_cap_pct` (`:59-60`) — because `rfm_snapshots` stores no cap and no
params (`db/migrations/010_analytics_rfm.sql:6-16`), so a caller-chosen
cap row would be indistinguishable from a default one and its M cuts
unattributable from storage. Do not add a `--m-cap-pct` flag: a rendered
flag would re-open exactly that. Cap experiments belong on the live
endpoints, which persist nothing — and the GETs must stay unpinned for the
mirror-image reason: a live response **self-attributes**, returning the cap it
used inside the boundaries payload (`"m": {"m_cap_pct": …, "winsor_cap_cents": …}`,
`ml_engine/rfm.py:317`, validated `:292-295`), so a cap-chosen response states
its own M cuts and cannot be silently incomparable, while a stored row cannot.
Do not pin the GETs to the default either: that would delete the only place a
cap can be inspected before it is proposed as an engine default. The refusals
are covered by app tests since `0fb9fba` (mutation-checked: disable the branch
and the refusal answers 201).

**Two edges of that claim** (ml, re-checked in `ml_engine/rfm.py` at HEAD
rather than transcribed). (1) The cap is consulted **only on the quintile
path**: `score_rfm` validates and applies it in the `else` branch alone
(`:292-295`, reported `:317`), so a population below
`QUINTILE_MIN_POPULATION` (`:25`, 25 rows) takes `_fallback_scores`
(`:289`) and is stamped `method="threshold_fallback"` (`:290`) — the cap is
never read, and the `m` boundaries that path returns are median-relative
thresholds with no `m_cap_pct` key. Self-attribution therefore holds for
every response, but *about the cap* only on the quintile path; irrelevant at
920 customers, yet it is the honest scope of the sentence above, including
for the app refusals, which guard a parameter the engine may not consult.
(2) `DEFAULT_M_CAP_PCT` (`:26`) is covered by **no version string**:
`RULES_VERSION = "v1"` (`:24`) and `RULES_V2 = "v2"` (`store_ld8.py:36`)
version the segmentation ruleset, not the cap. Changing that constant is
therefore a **contract event (@both)**, not a refactor — every stored row's
M cuts would lose their stated derivation and no column records it.

**Scope is a run-level dimension, so no brand-scoped snapshot row exists.**
`--brand-id`/`--group-id` (`scripts/run_pipeline.py:130-131` → `JobCtx`,
`:113-114`) reach **every** job of a tick through `_scope_args(ctx)`
(`pipelines/schedule.py:255-263`), appended by `_with_common` (`:284-299`,
`with_scope` default True); a job's `params` cannot carry a scope, so
per-brand rows require **one runner invocation per brand** and that
invocation scopes the *whole* pipeline, not one job. The nightly cron passes
neither flag, so every stored row is hq-scoped (`brand_id IS NULL`) —
correct, not a gap. Consequence, verified live by app 2026-10-17 on
`dac99d9`: a brand-scoped session gets live RFM at `n=435` while
`GET /analytics/rfm/snapshots` returns **0 rows** and `movement` a **404
`need_two_snapshots`**, so the movement section cannot populate for that
role at all — **not a platform limit: nothing writes scoped rows and no UI
action calls the POST**, while a scoped POST itself works (app
`test_rfm_brand_scope`, `services/api/tests/test_rfm.py:380`, asserts `201`
for a brand-scoped capture). Because pairing takes the newest `rules_version`
**present** in scope, a scope whose only rows are v1 would pair v1-vs-v1 across
two dates, so a *scoped* capture path is viable where an hq one is not (at hq
a v1 capture loses to the v2 rows under that same rule). Find-or-create is scope-aware (`ml_engine/store_ld8.py:65-66`, the
`brand_id`/`group_id` conjunctions of the find predicate),
so a future per-brand fan-out would add rows beside the hq row rather than
replace it. Fan-out price on the demo dataset: **6 ticks, one per brand**
(`DEMO`/`SUNHING`/`KVLN`/`VICEW`/`HARBOR`/`PEAKLENS`), because the single
group makes a `--group-id` run customer-identical to hq — brand is the only
dimension that partitions anything here. No ML change is needed for it (the
aggregate is already scope-parameterized, `ml_engine/store_rfm.py:29-31`, and
`run_ld8` already threads `brand_id`/`group_id`), so enabling per-brand writes
is a **runner** decision, not an ML one — and it does **not** fix the scoped
role that night: the band pairs **two** `current` rows of the newest
`rules_version` in scope, so each scope needs a second scoped night before it
can populate — and the accurate copy is scope-neutral (*"no movement for
this scope yet — movement appears once two snapshots of this window exist
for this scope"*), because "the view needs a second night" is true of a
hq/unscoped session and **false of a scoped one**: a second night still
writes hq-stamped rows while the nightly's manifest scope stays hq-only,
so the scope's own row count never moves.

**Group scope is conjunctive by design, and its only silent exclusion is
pre-group customers.** A group run requires `customers.group_id = g`
**and** `brands.group_id = g` (scope is the group's shared pool *and* the
brands inside it), matching `db/migrations/011_groups.sql:6` —
"Customers are shared within a group (customers.group_id) with NO linkage"
across clients (hard silo at the group level). `brands.group_id` is NOT NULL
(`:23`), so the brand half can never be NULL-matched away;
`customers.group_id` is NULLABLE on purpose (`:86`), the same legacy pattern
as `consents.brand_id` / `NULL = pre-brand` (`:95-99`), and a group-scoped
run **excludes** those rows silently. Checked against the live demo dataset
2026-10-17: 1 group (`DEMO`), 6 brands, all in it, and **0 of 1208 customers
with a NULL `group_id`** — so the customer half is a no-op here and the
predicate cannot be exercised on demo data (a `--group-id DEMO` run is
customer-identical to hq). Treat it as a contract property, not a testable
one: it only becomes a live edge on operator data with pre-group customers.
Nobody asserts the exclusion either — ML's suite is pure/DB-less by design, and
app's group tests cover CRUD (`services/api/tests/test_groups.py:90`), the seed
backfill (`:133`), cross-brand 403 + group-wide managers (`:155`), silo and
unlinkable defaults (`:217`), tier brand scope (`:287`) and staff-link defaults
(`:331`) — none of them puts a NULL-`group_id` customer under a group-scoped
run, so this predicate is a property with **no coverage in either suite**; a
fixture would have to be added app/data-side if it is ever challenged.
The switch that would **include** those rows instead of dropping them is to
swap the aggregate's guard for the `IS NOT DISTINCT FROM` form. The aggregate
filters by `AND ($4::uuid IS NULL OR c.group_id = $4::uuid)`
(`ml_engine/store_rfm.py:30`, with `brand_id` at `:29`) — the `IS NULL OR`
guard sits on the **parameter**, so a supplied scope leaves plain equality
against a NULLable column and drops those rows — whereas the *listing* query
already uses `brand_id IS NOT DISTINCT FROM $3::uuid` / `group_id IS NOT
DISTINCT FROM $4::uuid` at `store_rfm.py:58-59`, where unfiltered selection is
the goal; the two must not be confused for each other — and the discriminator
is **artifact vs population**, not selection vs filtering (ml, 2026-10-17):
`IS NOT DISTINCT FROM` **locates a stored artifact by its recorded scope**
(a NULL stamp means "unscoped artifact" and must match, or the row you are
about to find/create is invisible), while the `IS NULL OR` guard **scopes a
live population**, where a NULL group means unassigned and must drop. All 11
`IS NOT DISTINCT FROM` predicate pairs in `ml_engine/` are the former
(`SNAPSHOT_FIND_SQL` `store_churn.py:193-198`, guarding `:196-197`, then
`:301-302`, `:311-312`, `:323-324`, `store_clv.py:168-169`,
`store_forecast.py:31-32`, `store_ld8.py:65-66`, `store_recs.py:88-89`,
`:103-104`, `store_rfm.py:58-59`, `store_segments.py:388-389`); the guard's
*reads* are the latter — `CUSTOMER_TX_ORDERS_SQL` / `CUSTOMER_TX_ACTIVITY_SQL`
(`store_churn.py:253-261`, guarding `:258-259`, `:263-281`) scope per-customer
transaction history for the BG/NBD fit rather than locating a stored row. One
file carries both sides over the same two scope columns with opposite
operators (`store_churn.py:193-198` vs `:253-261`), so artifact-vs-population
is the only variable in that comparison — the form to cite when the question
is which operator a query that does not exist yet should use (ml/app,
2026-10-17).

The same split has a second, independent statement on the app side, and it is
the reference *decision* rather than an implementation detail:
`../crm-cdp-app/services/api/analytics/scope.py:1-26` (docstring) — a stamped
row is visible iff `row.brand == session.brand AND row.group == session.group`,
NULL-safe on both sides — with the deliberate consequence named there,
**quarantine-by-default**: legacy pre-scope rows and admin-created global
batches are `NULL/NULL` and therefore **HQ-only**, so no group session can
list or open a row computed over all brands. That consequence is the one data's
writers live under: an unstamped write is not an error in this system, it is a
row only HQ can see. The docstring's own invariant is "keep the two in sync —
`row_visible` is the reference implementation" (`scope.py:46-50`). The failure
direction that matters, verified 2026-10-17 by app's sweep and re-derived here:
a **stamped** surface using the population form would leak `NULL/NULL` rows
into a brand session — a visibility bug, not a semantics curiosity; none in
app's tree, whose population-form hits are all source-row filters over
`orders`/`customers`/`brands`/`groups` (`analytics_snapshots.sql:110-111`,
`analytics_simulate.sql:16-18`, `analytics_recs_fallback.sql`, `stats.py:23-29`
`_ORDER_SCOPE`/`_CUST_SCOPE`). **Granularity, sharpened to its smallest case:** the discriminator is
**per column, read over a specific row — "is this column the row's scope
stamp?"** — not per file, not per table, and not even per statement. The
smallest instance carries both forms in one statement over one row:
`SNAPSHOTS_LIST_SQL` (`store_rfm.py:56-59`) asks population-form for `mode`
(`:57`, `$1::text IS NULL OR mode = $1`, where NULL means "every mode") and
artifact-form for the scope stamp (`:58-59`, `brand_id`/`group_id IS NOT
DISTINCT FROM`), so row role cannot be the variable there. The file-level
counterpart is `analytics_snapshots.sql` — artifact form on the stamped row
(`:22-23`), population form on the joined dimension rows (`:110-111`) — which
is why a per-file label ("this query file is artifact-form") would annotate
one of those two lines wrongly.
— **declined on purpose, not missed**: 011's design is exclusion, and a group
run means the group's pool, not "the pool plus unassigned rows". Recorded so
the choice is visible rather than rediscovered.

**Third writer, non-scheduled:** `POST /analytics/rfm/snapshots` writes
**v1** through the same `store_rfm.create_snapshot`, stamped
`created_by=user.staff_code` (`analytics_rfm.py:132-142`). It is an
ordinary v1 append, so it changes the v1 row inventory but not the
pairing rule above, and with the cap pinned it cannot store a fit the v1
engine path could not express.

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

Segments k: the seed pins `k: 5` EXPLICITLY on every `engine:
segments` job, because serving reads the LATEST run for (mode, k,
scope) and its own default is `k=5` (app segment/explain routers,
`k: Query(ge=2, le=20) = 5`). An unwritten k is not a soft miss: the
read answers 404 `no_snapshot` with `available_k`. A cadence that only
ever fits k=6 therefore leaves the product's default surface dark
right after a wipe while every job reports succeeded -- and the
product exposes exactly ONE k (no picker; `segments/page.tsx:561-564`),
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
change to **this repo's runner only, zero ML change**. Two halves are
open and they are different problems: the **feature** (015 gives
manifests + `pipeline_runs`, but nothing in the product schedules a tick
or shows one — app, after 015) and the **infrastructure** (nothing fires
the tick at all: no cron entry on any host, no in-repo loop; verified
2026-10-17). Do not read a green `pipeline_runs` row as proof a scheduler
exists — those rows are hand-runs.

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

That containment is the whole dispatch, not just the resolver: the
resolve call and `ENGINE_BUILDERS[engine](...)` both sit inside the job's
`try` (`pipelines/schedule.py:634-641`), so a malformed manifest
(a wrong `params` key raising in `_build_matrix`, an `argv` builder
refusing its input) also lands as `status = 'failed'` with
`type(exc).__name__: exc` on that job's `pipeline_runs` row (`:658`)
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
(`services/api/tests/test_schedule.py:415-424` pins all three):

| engine | receives `--brand-id`/`--group-id`? | receives `--created-by`? |
|---|---|---|
| `explain_batch` | **no** — scope rides the resolved segment run (`pipelines/schedule.py:426-444`, `_with_common(..., with_as_of=False, with_scope=False)`) | yes |
| `matrix_batch` | **no** — same reason | **no** (argparse declares none) |
| `assoc_batch` | **yes** — reads the catalog directly and no row carries a scope for it (`pipelines/schedule.py:410-423`, `with_scope` default) | yes |

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
/ `validate_segment_size` + the UPSERT/SELECT SQL.
