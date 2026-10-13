# DB + API Conventions & PII Policy (enforceable, short)

## Naming

- Tables: plural `snake_case` (`customers`, `customer_contacts`, `consents`,
  `consent_events`, `stores`, `products`, `orders`, `order_items`, `customer_access_logs`).
- Columns: `snake_case`. PK `id uuid DEFAULT gen_random_uuid()`.
  FK `<singular>_id uuid NOT NULL REFERENCES <table>(id)`.
  Timestamps `created_at / updated_at / deleted_at timestamptz` (`at`/`captured_at` on event rows).
- JSON over the wire is `snake_case` 1:1 with column names — no mapping layer.
  Pydantic v2 models mirror row shapes; `attributes JSONB` is verbatim round-trip.
- Migrations: `db/migrations/NNN_name.sql`, dbmate format
  (`-- migrate:up` / `-- migrate:down`), additive only (no destructive DDL).
- Queries: `db/queries/<resource>.sql`, see `_template.sql` for the named-query convention.

## Master data (reference_values)

- New controlled vocabularies (channels, statuses, kinds, roles, ...) default to
  rows in `reference_values(domain, code, label_en, label_zh, sort_rank, is_active)` —
  do NOT add Pydantic `Literal` enums or per-domain `CHECK` constraints for them.
- Graduate a domain to its own table only when it grows real fields/behaviour
  beyond (code, labels, rank, active) — e.g. `membership_tiers` earned a table
  via rank + benefits_text.
- Validation is a DB lookup (`services/api/repos/reference.py::require_active`);
  unknown codes fail with 422 whose detail names the domain
  (`{"code": "unknown_<domain>", "domain": ..., "value": ...}`).
- Ordering that carries meaning (campaign `draft -> active -> ended`) is encoded
  in `sort_rank` and the transition logic reads it — never a second hardcoded copy.
- Read API: `GET /reference/domains`, `GET /reference?domain=<d>`
  (active codes only, ranked by `sort_rank, code`).
- API base `/api/v1`. `POST /customers/search` (body) for PII lookup;
  `GET` only for UUID paths, `member_id` fetch, product search.

## PII policy (MUST)

1. Phone/email match by **exact normalized value only** — E.164 (`phonenumbers`, default region HK)
   for phone, lowercase+trim for email. No LIKE/fuzzy on PII. Name search may use `pg_trgm`.
2. **Never** put phone/email in URL, query string, or browser history — body-only search.
3. **Mask** contacts in multi-match lists (e.g. `+852 ****4567`, `j***@example.com`).
   Data-owner override 2026-10-08: staff sessions see FULL values — mask
   helpers retained but unused (list endpoints return the primary FULL value).
4. **No request-body logging** on `/customers/search` (route-level log filter in `deps.py`).
5. `Cache-Control: no-store` on all PII routes (search, profile, consents, orders).
6. **Rate-limit** search per session `staff_code` (`x-session-token` identity) → `429` + log.
7. Every search appends `customer_access_logs(staff_id, criteria_type, result_count, at)` —
   criteria **type** only, never raw PII values. `consent_events` / `customer_access_logs`
   are **append-only** (no UPDATE/DELETE grants; enforced in migrations).
8. Soft-delete customers (`deleted_at`); hidden from search/reads, history retained.
