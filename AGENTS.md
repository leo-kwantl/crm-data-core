# crm-cdp-data — ingestion pipeline (what lands rows)

**You are `crm-data`** (team **data**): owns `pipelines/**`, `db/migrations/**`, `db/seeds/**` and `pipelines/contracts.md`. Peers: `crm-app` (serves) and `crm-ml` (scores). Never edit `../crm-cdp-app` or `../crm-cdp-ml`.

## Run / verify

```bash
python ../tools/docs_check.py --repo crm-cdp-data
pip install -r services/api/requirements.txt
python -m pytest services/api/tests/test_orders.py services/api/tests/test_customer_events.py services/api/tests/test_identity.py -q -p no:cacheprovider
python -c "import pipelines.ingest.orders, pipelines.ingest.events, pipelines.ingest.identity"
```

## Repo rules

- `member_id` must match `^M-[0-9]{6}$` — reject + quarantine, never coerce. `orders.store_id` NOT NULL (resolve store first).
- The seed space is two writers: `db/seeds/demo.sql` (eight curated `M-000101..108`, fixed `c0000000-…-0000000N` ids, stores `CWB-01`/`TSM-02`) + `scripts/seed_reseed_1200.py` (`M-03%`). "Not like `M-03%`" is not a debris test — it never reaches zero.
- `customers` has no `brand_id`: brand reaches a customer through `orders`/`stores`, so a leftover customer is visible from every brand holding one of its orders and invisible from the rest — a brand-isolation test must scope customers through orders.
- Events/consents append-only; dedup key `(source, source_event_id)`; money in integer cents.
- The order timestamp is `ordered_at` — there is **no** `orders.placed_at`, so a check that greps for that name passes on data it never read.
  No fact may be dated after `now()`: `apply` quarantines a *completed* future order as `future_dated`, seeds date every fact `now()`-relative, and `services/api/tests/test_no_future_facts.py` holds both rules.
- A time bound takes its form from the column's type: a `timestamptz` anchor gets the **next business day, exclusive** (`>= ((($1::date) + 1)::timestamp AT TIME ZONE interval '8 hours')`), a `date` anchor gets `> $1::date`.
  Never compare an instant column to a bare date — the date is promoted at the session zone, so `> today` lands at the *start* of the day and the day's own rows read as future.
  Cast `$1::date` explicitly (a bare `$1` is an unknown parsed at the session zone). Third repo to hit this after ml's `forecast._as_of_bound` and app's `test_observed_anchor.py`; data's implementation is `FUTURE_DATED_INSTANT` in `pipelines/retention.py`.
- Owns future `01x_ingest_*` migrations.
  For any file present in **both** trees, app's checkout is the base (`db/`, `services/api/**`) — sync byte-for-byte, never fork; never delete a data-only harness file.
  `services/api/tests/**` is data's own harness and is **not** in a sync's scope; `pipelines/**` is data's (app vendors `pipelines/ingest/connectors.py` for serving).
  A mirrored module must be mirrored with the files it reads at import (`db/queries/*.sql`), or the tree imports and cannot boot.
  See `pipelines/contracts.md` §"Tree ownership & the mirror boundary".
- `scripts/bootstrap_db.py` is DEV/CI ONLY — NEVER run against a live DB (TRUNCATEs every table + reseeds demo; live = migrate-only).
- `pytest` writes freely and does not clean up after every fixture, so `DATABASE_URL` is refused unless `CRM_CDP_ALLOW_SHARED_DB=1`; an opted-in run is audited and fails on any table's row-count drift (`services/api/tests/shared_db_guard.py`). Debris a past shared run left behind: `python scripts/clean_test_debris.py` (dry run; `--apply` dumps the rows first).
- `db/migrations/` is applied in filename order and the version key is the file stem; a number is never reused and dbmate is not installed — `../docs/conventions.md` § Naming.
- After editing `pipelines/contracts.md`, run `python scripts/audit_doc_cites.py`: it checks every cite against the trees it names.

## Contracts

- A new source/field updates `pipelines/contracts.md` **first**, then announces `[BREAKING|ADDITIVE|FYI] <scope> branch+migration`. Data-owned BREAKING scopes: ingest-required / dedup / resolution / snapshot-math / migration = BREAKING for `@crm-app` + `@crm-ml`; ML CLI-argv only → DM `crm-ml`; a two-party change → DM. Cross-repo rules: `../AGENTS.md`.

## PII

- Policy is cross-repo: `../docs/conventions.md` § PII policy (exact-first, append-only audit, soft delete).
- Data-side normalization + the searchable-column contract: `docs/conventions.md`.

## Environment

- Runtime traps (cmd shell: `set "VAR=..." && ...`, no `export`; pgembed; the one-backend rule; login-vs-health): `../docs/environment.md`. Data adds none.

## Cross-team

- Protocol → `../AGENTS.md` § Cross-team work; edge cases → `../docs/agent-comms.md` — agent-comms and `codegraph` are optional externals, skip if not installed. Data deltas: `register crm-data` visible; join `data-contracts`; `dm_admit` crm-app/crm-ml once; `read_room` on start.

## Conventions

- `docs/conventions.md` — data deltas only: PII normalization + the searchable-column contract.
- Doc gate: `.githooks/pre-commit` and `.githooks/pre-push` run `python ../tools/docs_check.py --repo crm-cdp-data` — a doc that breaks the rules is neither committed nor pushed, and `--no-verify` is not the way past it.
- Cross-repo conventions — naming, master data, git history, PII policy (`../docs/conventions.md`) and the cross-team protocol (`../docs/agent-comms.md`) — are owned by the manager at `../AGENTS.md`; read it, do not retype it.
