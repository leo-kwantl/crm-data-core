# crm-cdp-data conventions (data deltas)

Cross-repo practices are **owned by the manager**: read `../../AGENTS.md` for the everyday rules, `../../docs/conventions.md` for naming, master data, git history and PII policy, `../../docs/agent-comms.md` for the cross-team protocol. Only data's deltas live here — retyping the canonical text is how these mirrors drifted.

A conventions file carries norms only: no pointers, no interface listings, no restatement of a rule that lives somewhere else.

## PII normalization (data side)

- Normalization: phone → E.164 (`phonenumbers`, default region HK); email → lowercase+trim.
- Searchable column is `customer_contacts.value_normalized` (phone digits-normalized, email lowercased+trimmed); `value_display` never in a predicate.
- Exact-first. Override 2026-10-13 (owner): after an exact miss, phone/email fall back to a bounded substring match (min 3 digits / 3 chars, LIMIT 20, POST body, audited criteria-type-only), exact-first order preserved.
- Override 2026-10-08 (owner): staff sessions see FULL contact values — the mask helpers are retained but unused.
- Demographic PII (2026-10-14, `017_customers_demographics`): `date_of_birth` and `address_line` follow the same body-only, exact-match rule as phone/email;
  `gender` / `district` are quasi-identifiers, never combined with PII in one GET predicate; age is derived (`age(date_of_birth)`), never stored.
- Identity search: `external_member_id` + `member_id` substring fallback (escaped ILIKE, LIMIT 20).
- The append-only grants (`consent_events`, `customer_access_logs`) and customer soft-delete are enforced in data-owned migrations; the policy they implement is `../../docs/conventions.md` § PII policy.
