# crm-cdp-data — ingestion pipeline (what lands rows)

**You are `crm-data`** (team **data**): owns `pipelines/**`, `db/migrations/**`, `db/seeds/**` and `pipelines/contracts.md`. Peers: `crm-app` (serves) and `crm-ml` (scores). Never edit `../crm-cdp-app` or `../crm-cdp-ml`.

## Run / verify

```bash
pip install -r services/api/requirements.txt
python -m pytest services/api/tests/test_orders.py services/api/tests/test_customer_events.py services/api/tests/test_identity.py -q -p no:cacheprovider
python -c "import pipelines.ingest.orders, pipelines.ingest.events, pipelines.ingest.identity"
```

## Repo rules

- `member_id` must match `^M-[0-9]{6}$` — reject + quarantine, never coerce. `orders.store_id` NOT NULL (resolve store first).
- Events/consents append-only; dedup key `(source, source_event_id)`; money in integer cents.
- Owns future `01x_ingest_*` migrations.
  For any file present in **both** trees, app's checkout is the base (`db/`, `services/api/**`) — sync byte-for-byte, never fork; never delete a data-only harness file.
  `services/api/tests/**` is data's own harness and is **not** in a sync's scope; `pipelines/**` is data's (app vendors `pipelines/ingest/connectors.py` for serving).
  A mirrored module must be mirrored with the files it reads at import (`db/queries/*.sql`), or the tree imports and cannot boot.
  See `pipelines/contracts.md` §"Tree ownership & the mirror boundary".
- `scripts/bootstrap_db.py` is DEV/CI ONLY — NEVER run against a live DB (TRUNCATEs every table + reseeds demo; live = migrate-only).
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
- Cross-repo conventions — naming, master data, git history, PII policy (`../docs/conventions.md`) and the cross-team protocol (`../docs/agent-comms.md`) — are owned by the manager at `../AGENTS.md`; read it, do not retype it.
