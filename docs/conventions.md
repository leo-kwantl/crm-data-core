# crm-cdp-data conventions

Cross-repo practices are **owned by app**: read `../crm-cdp-app/docs/conventions.md`
for naming, lists, master data, git history, PII policy and § Cross-team work
(agent-comms). Only data's deltas live here — retyping the canonical text is how
these mirrors drifted.

## PII normalization & data-side deltas

- Normalization: phone → E.164 (`phonenumbers`, default region HK); email → lowercase+trim.
- Searchable column is `customer_contacts.value_normalized` (phone digits-normalized,
  email lowercased+trimmed); `value_display` never in a predicate.
- Exact-first. Override 2026-10-13 (data owner): after an exact miss, phone/email fall
  back to a bounded substring match (min 3 digits / 3 chars, LIMIT 20, POST body,
  audited criteria-type-only), exact-first order preserved.
- Override 2026-10-08 (data owner): staff sessions see FULL contact values — the mask
  helpers are retained but unused.
- Demographic PII (2026-10-14, `017_customers_demographics`): `date_of_birth` and
  `address_line` follow the same body-only, exact-match rule as phone/email;
  `gender` / `district` are quasi-identifiers, never combined with PII in one GET
  predicate; age is derived (`age(date_of_birth)`), never stored.
- Append-only grants (enforced in migrations): `consent_events`,
  `customer_access_logs` — criteria **type** only, never raw PII values. Customers are
  soft-deleted (`deleted_at`); history retained.
- Identity search: `external_member_id` + `member_id` substring fallback (escaped
  ILIKE, LIMIT 20).

## Cross-team work — data deltas

- Announce in `data-contracts`: `[BREAKING|ADDITIVE|FYI] <scope> branch+migration`.
  ingest-required / dedup / resolution / snapshot-math / migration = BREAKING for both
  peers, and `pipelines/contracts.md` is updated first.
- ML CLI argv only → DM `crm-ml`. Two-party changes → DM.
