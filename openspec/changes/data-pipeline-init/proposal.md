# Data pipeline init

Stand up the ingestion pipeline as its own repo (`crm-cdp-data`), split out
of the `crm-cdp` monorepo. App (`crm-cdp-app`) owns serving DDL + API;
data owns everything that lands rows.

## Scope

- `pipelines/ingest/{orders,events,identity}.py` normalize + validate (done, stub-tested next).
- `pipelines/contracts.md` field mapping + idempotency keys (done).
- Next: staging tables + quarantine, CDC/outbox design, backfill runner,
  per-source connectors, row-count reconciliation vs app.
- Out of scope: API serving, ML scoring, web UI.

## Verification

- `pytest services/api/tests/test_orders.py services/api/tests/test_customer_events.py services/api/tests/test_identity.py` green.
- Pipeline `normalize()` unit tests for reject paths (bad member_id, null store, empty lines).
