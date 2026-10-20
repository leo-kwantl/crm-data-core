# Data integration pipeline (CRM-CDP ingest)

Owns everything that lands rows in CRM core tables. The API (`crm-cdp-app`)
and ML (`crm-cdp-ml`) consume; they never write ingestion logic.

## Contracts (mirror of `crm-cdp-app`, app is source of truth for DDL)

- `customers.member_id` must match `^M-[0-9]{6}$` (CHECK). Reject, don't coerce.
- `orders.store_id` NOT NULL — resolve/create the store before the order.
- Identity: `external_member_id` + `member_id` substring fallback (escaped
  ILIKE, LIMIT 20); phone/email are exact-first — after an exact miss a bounded
  substring fallback applies to POST search only (min 3 digits / 3 chars,
  LIMIT 20, criteria-type-only audit; `../../docs/conventions.md`).
- Events: append-only (`customer_events`, `consents`); never UPDATE/DELETE.
- All migrations additive-only (`db/migrations`, plain SQL — applied in filename
  order, version key = file stem; dbmate is not installed).

## Layout

- `pipelines/ingest/orders.py` — order ingest: validate store, normalize lines, upsert idempotently.
- `pipelines/ingest/events.py` — customer/consent event append path.
- `pipelines/ingest/identity.py` — external-ID binding + member-id resolution.
- `pipelines/contracts.md` — field-level source→table mapping + idempotency keys.
- `db/` — full migration mirror (app owns `001-009`-era base; data owns future `01x_ingest_*`).
- `services/` — API harness mirror so ingestion tests run standalone (app owns).

## Run

```bash
pip install -r services/api/requirements.txt
python -m pytest services/api/tests/test_orders.py services/api/tests/test_customer_events.py services/api/tests/test_identity.py
```

## Roadmap

CDC/outbox from source systems → staging tables → validated upsert. See
`openspec/changes/data-pipeline-init/proposal.md`.

## Status — missing pieces (documented, NOT built; follow-ups)

- **Quarantine:** no quarantine tables and no quarantine write path —
  `normalize()` raises `ValueError` today; the contracts.md "rejects go to
  quarantine" rule has no backing store or code yet.
- **Staging tables:** none exist; loads validate inline, no land-then-validate step.
- **CDC/outbox:** no source-system CDC or outbox consumer exists.
- **Backfill/reconciliation:** no backfill runner, no source-vs-table
  reconciliation job.
- **Reject-path unit tests:** `normalize()` happy paths are exercised via
  the API harness tests, but there are no dedicated unit tests for the
  `ValueError` reject paths (bad member_id, missing store, empty lines,
  missing dedup keys).

## Shipped work

The dated record of what landed is in `pipelines/CHANGELOG.md` — a log, not a doc.
