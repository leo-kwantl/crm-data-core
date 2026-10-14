# crm-cdp-data — ingestion pipeline (what lands rows)

Owns `pipelines/ingest/{orders,events,identity}.py` + `pipelines/contracts.md`. App serves, ML scores.

```bash
pip install -r services/api/requirements.txt
pytest services/api/tests/test_orders.py services/api/tests/test_customer_events.py services/api/tests/test_identity.py -q -p no:cacheprovider
```

- `member_id` must match `^M-[0-9]{6}$` — reject + quarantine, never coerce. `orders.store_id` NOT NULL (resolve store first).
- Events/consents append-only; dedup key `(source, source_event_id)`; money in integer cents.
- Owns future `01x_ingest_*` migrations; `db/` + `services/` mirror app (app owns base — sync, don't fork).
- Never edit `../crm-cdp-app`, `../crm-cdp-ml`.
- `scripts/bootstrap_db.py` is DEV/CI ONLY — NEVER run against a live DB (TRUNCATEs 11 tables + reseeds demo; live = migrate-only).
- Multi-pi (all optional/external): `register crm-data` visible (agent-comms, only if installed), room `data-contracts` (only if daemon runs; ingest-required/dedup/resolution/snapshot-math/migration = BREAKING @both, `contracts.md` first; ml-argv-only → `dm @crm-ml`; 2-party → `dm`); `dm_admit` crm-app/crm-ml once; review = directed `dm @target+branch`; `read_room` on start; `codegraph query <sym>` (external CLI, only if installed). New source/field → update `contracts.md` first, then announce as `[BREAKING|ADDITIVE|FYI] <scope> branch+migration`.
- Owner rule 2026-10-14: delegated / handed-off / in-room-requested tasks are PRE-APPROVED — build without asking, announce each delivery with branch+migration per protocol, never wait for a per-item GO.
- See `pipelines/README.md`, `pipelines/contracts.md`, `openspec/changes/data-pipeline-init/`.

## Git history & commit convention (shared across crm-cdp-app / -data / -ml)

- Day-zero trees land as ONE import commit: `chore: initial import of
  <repo> monorepo`. Never fabricate granular history after the fact.
- From the import forward, one logical change per commit, Conventional
  Commits: `feat|fix|docs|test|refactor|chore(scope): imperative subject`
  (scope = area, e.g. `ingest`, `snapshots`, `upload`, `scheduler`).
  Contract changes pair branch + migration per the protocol above.
- No remote yet at import time: rewriting is free until the first push.
  After push, history is immutable.

## PII search rule (mirrored from app conventions.md — app owns the text)

- Phone/email match by **exact normalized value only**; no LIKE/fuzzy on
  PII. Override 2026-10-13 (owner): after an exact miss, phone/email
  fall back to a bounded substring match (min 3 digits / 3 chars,
  LIMIT 20, POST body, audited criteria-type-only). Exact-first
  ordering preserved.
- Data-side contract: the searchable column is
  `customer_contacts.value_normalized` (phone digits-normalized, email
  lowercased+trimmed); `value_display` never in a predicate.
