# crm-cdp-data — ingestion pipeline (what lands rows)

**You are `crm-data`** (team **data**): owns `pipelines/**`, `db/migrations/**`, `db/seeds/**` and `pipelines/contracts.md`. Peers: `crm-app` (serves) and `crm-ml` (scores). Never edit `../crm-cdp-app` or `../crm-cdp-ml`.

## Run / verify

```bash
python ../tools/docs_check.py --repo crm-cdp-data
pip install -r services/api/requirements.txt
python -m pytest services/api/tests -q -p no:cacheprovider
python -c "import pipelines.ingest.orders, pipelines.ingest.events, pipelines.ingest.identity"
```

- Run the whole directory, never a file list: three named files report `21 passed` for a suite that takes ten minutes, and a file named alone can skip where a full run fails (`test_catalog_vocabulary.py` reads the live vocabulary, which only exists once another file has seeded it).
- The last line is a **subset** and cannot see residue or second-run collisions — a green there means the files run, not that the suite is healthy.
- That health claim needs the full suite twice on a pristine cluster (`1fdd81b` is the measurement).
- `test_apply.py` purges its own namespace on module teardown and asserts nothing future-dated survives it.

## Repo rules

- `member_id` must match `^M-[0-9]{6}$` — reject + quarantine, never coerce.
- `orders.store_id` NOT NULL (resolve store first).
- The seed space is two writers — `db/seeds/demo.sql` (eight curated `M-000101..108`, fixed `c0000000-…-0000000N` ids, stores `CWB-01`/`TSM-02`) and `scripts/seed_reseed_1200.py` (`M-03%`) — so "not like `M-03%`" is not a debris test: it never reaches zero.
- `customers` has no `brand_id`: brand reaches a customer through `orders`/`stores`, so a leftover customer is visible from every brand holding one of its orders and invisible from the rest — a brand-isolation test must scope customers through orders.
- A test file that writes to a database purges its own rows in a `finally`, matched by the predicate that created them, and seeds outside `demo.sql`'s space (`M-000101..108`, `S101`, `FRM-/CLT-/CSE-1xx`) — a fixed id there fails the *first* run on a demo-seeded cluster.
- A throwaway-cluster green is not evidence about residue: `conftest.py` disarms the row-count audit unless `DATABASE_URL` is the shared one.
- Where the table is append-only by `REVOKE` (`customer_events`, `customer_access_logs`), deletion cannot be the rule, so those files owe **scope**: write outside the seeded space and bound every assertion to the id just created, so no check counts a global total a previous run could move.
- A script executed by a test is test code: `importlib.util.spec_from_file_location` loads `scripts/run_upload.py` (`services/api/tests/test_apply.py:19`) and `scripts/repair_created_by_2026_10_20.py` (`services/api/tests/test_created_by_repair.py:23`) by path on every run, so a script's **module scope stays side-effect free** — a `SystemExit`, a connection or a `raise` at import breaks the suite from a file nobody thinks of as test-adjacent, and `grep import scripts` will not see it (grep `spec_from_file_location|importlib|runpy|subprocess` instead).
- Events/consents append-only.
- Dedup key `(source, source_event_id)`.
- Money in integer cents.
- The order timestamp is `ordered_at` — there is **no** `orders.placed_at`, so a check that greps for that name passes on data it never read.
- No fact may be dated after `now()`: `apply` quarantines a *completed* future order as `future_dated`, seeds date every fact `now()`-relative, and `services/api/tests/test_no_future_facts.py` holds both rules.
- A time bound takes its form from the column's type: a `timestamptz` anchor gets the **next business day, exclusive** (`>= ((($1::date) + 1)::timestamp AT TIME ZONE interval '8 hours')`), a `date` anchor gets `> $1::date`; data's implementation is `FUTURE_DATED_INSTANT` in `pipelines/retention.py`.
- Never compare an instant column to a bare date — the date is promoted at the session zone, so `> today` lands at the *start* of the day and the day's own rows read as future. Third repo to hit this after ml's `forecast._as_of_bound` and app's `test_observed_anchor.py`.
- Cast `$1::date` explicitly (a bare `$1` is an unknown parsed at the session zone).
- Owns future `01x_ingest_*` migrations.
- For any file present in **both** trees, app's checkout is the base (`db/`, `services/api/**`) — sync byte-for-byte, never fork; never delete a data-only harness file.
- A mirrored module must be mirrored with the files it reads at import (`db/queries/*.sql`), or the tree imports and cannot boot.
- `services/api/tests/**` is data's own harness and is **not** in a sync's scope.
- `pipelines/**` is data's (app vendors `pipelines/ingest/connectors.py` for serving).
- `scripts/bootstrap_db.py` is DEV/CI ONLY — NEVER run against a live DB (TRUNCATEs every table + reseeds demo; live = migrate-only).
- `pytest` writes freely and does not clean up after every fixture, so `DATABASE_URL` is refused unless `CRM_CDP_ALLOW_SHARED_DB=1`.
- An opted-in run is audited and fails on any table's row-count drift (`services/api/tests/shared_db_guard.py`).
- `db/migrations/` is applied in filename order and the version key is the file stem; a number is never reused — `../docs/conventions.md` § Naming.
- dbmate is not installed.
- After editing `pipelines/contracts.md`, run `python scripts/audit_doc_cites.py`: it checks every cite against the trees it names.

## Contracts

- A new source/field updates `pipelines/contracts.md` **first**, then announces `[BREAKING|ADDITIVE|FYI] <scope> branch+migration`. Data-owned BREAKING scopes: ingest-required / dedup / resolution / snapshot-math / migration = BREAKING for `@crm-app` + `@crm-ml`; ML CLI-argv only → DM `crm-ml`; a two-party change → DM. Cross-repo rules: `../AGENTS.md`.

## PII

- Policy: `../docs/conventions.md` § PII policy (exact-first, append-only audit, soft delete); data's delta: normalization + the searchable-column contract, `docs/conventions.md`.

## Environment

- Runtime traps (pgembed, one-backend rule, login-vs-health; `cmd` is `/d /s /c`, no `export`): `../docs/environment.md` — and a run that resolves a target (a DSN, a cluster, a repo path) prints it as `host:port/db`, because measured 2026-10-20 the same command shape produced runs reporting different clusters: what you typed is not evidence of the target the process used.
- No script may default to a DSN — a default is a silent target, and a `DELETE`/`UPDATE` aimed at the wrong cluster is the one mistake a second run cannot fix: resolve from `DATABASE_URL` only, refuse when it is unset, and print `host:port/db` before the first write (`scripts/clean_test_debris.py`, `scripts/repair_created_by_2026_10_20.py`).

## Cross-team

- Protocol → `../AGENTS.md` § Cross-team work; edge cases → `../docs/agent-comms.md` — agent-comms and `codegraph` are optional externals, skip if not installed. Data deltas: `register crm-data` visible; join `data-contracts`; `dm_admit` crm-app/crm-ml once; `read_room` on start.

## Conventions

- Doc gate: `.githooks/pre-commit` and `.githooks/pre-push` run `python ../tools/docs_check.py --repo crm-cdp-data` — a doc that breaks the rules is neither committed nor pushed, and `--no-verify` is not the way past it.
- Cross-repo conventions — naming, master data, git history, PII policy (`../docs/conventions.md`) and the cross-team protocol (`../docs/agent-comms.md`) — are owned by the manager at `../AGENTS.md`; read it, do not retype it.
