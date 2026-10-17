# crm-cdp-data — ingestion pipeline (what lands rows)

**You are `crm-data`** (team **data**): owns `pipelines/**`, `db/migrations/**`, `db/seeds/**`
and `pipelines/contracts.md`. Peers: `crm-app` (serves) and `crm-ml` (scores). Never edit
`../crm-cdp-app` or `../crm-cdp-ml`.

```bash
pip install -r services/api/requirements.txt
pytest services/api/tests/test_orders.py services/api/tests/test_customer_events.py services/api/tests/test_identity.py -q -p no:cacheprovider
```

- `member_id` must match `^M-[0-9]{6}$` — reject + quarantine, never coerce. `orders.store_id` NOT NULL (resolve store first).
- Events/consents append-only; dedup key `(source, source_event_id)`; money in integer cents.
- Owns future `01x_ingest_*` migrations. For any file present in **both** trees, app's checkout is the base (`db/`, `services/api/**`) — sync byte-for-byte, never fork; never delete a data-only harness file. `services/api/tests/**` is data's own harness and is **not** in a sync's scope; `pipelines/**` is data's (app vendors `pipelines/ingest/connectors.py` for serving). A mirrored module must be mirrored with the files it reads at import (`db/queries/*.sql`), or the tree imports and cannot boot. See `pipelines/contracts.md` §"Tree ownership & the mirror boundary".
- `scripts/bootstrap_db.py` is DEV/CI ONLY — NEVER run against a live DB (TRUNCATEs 11 tables + reseeds demo; live = migrate-only).
- **Cross-team work** (rooms, DMs, full-id rule, what is worth sending, delivery line, pre-approval, recap) → `docs/conventions.md` § Cross-team work — agent-comms and `codegraph` are optional externals, skip if not installed. Data deltas: `register crm-data` visible; join `data-contracts`; `dm_admit` crm-app/crm-ml once; `read_room` on start. A new source/field updates `pipelines/contracts.md` **first**, then announces `[BREAKING|ADDITIVE|FYI] <scope> branch+migration`.
- **`docs/conventions.md`** — this repo's deltas (PII normalization + the data-side searchable-column contract, cross-team data deltas). Canonical cross-repo text — naming, lists, master data, git history, PII policy, § Cross-team work — is owned by app at `../crm-cdp-app/docs/conventions.md`; read it, do not retype it.
- See `pipelines/README.md`, `pipelines/contracts.md`, `openspec/changes/data-pipeline-init/`.
