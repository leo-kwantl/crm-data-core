-- migrate:up
-- 028: LM1 fitted BTYD params (data, [ADDITIVE] analytics, joint w/ ml).
--
-- Data lands the table FIRST; ml's fitting engine writes rows that
-- match this grain. One row per (as_of x activity x horizon x scope x
-- model): the params snapshot behind every alive_path / prob_n /
-- expected-purchases read. Grain mirrors churn_snapshots (013) so the
-- serving read joins scope-to-scope. params holds {r, alpha, a, b}
-- (all > 0, enforced writer-side; NULL = fit failed, see
-- fit_error). Refit = new as_of row, never an UPDATE
-- (REVOKE UPDATE/DELETE, same convention as 013).

CREATE TABLE btyd_params (
  as_of date NOT NULL,
  activity text NOT NULL CHECK (activity IN ('orders', 'all')),
  horizon_days integer NOT NULL CHECK (horizon_days BETWEEN 1 AND 730),
  brand_id uuid NULL REFERENCES brands (id),
  group_id uuid NULL REFERENCES groups (id),
  model text NOT NULL DEFAULT 'bg-nbd'
    CHECK (model IN ('bg-nbd', 'pareto-nbd')),
  params jsonb NULL,
  n_customers integer NOT NULL DEFAULT 0 CHECK (n_customers >= 0),
  loglikelihood double precision NULL,
  fit_error text NULL,
  fitted_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((params IS NOT NULL)::int + (fit_error IS NOT NULL)::int >= 1)
);

-- One fit per day x activity x horizon x scope x model (NULL-safe).
CREATE UNIQUE INDEX btyd_params_grain_ux
  ON btyd_params (as_of, activity, horizon_days,
    COALESCE(brand_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(group_id, '00000000-0000-0000-0000-000000000000'::uuid),
    model);
CREATE INDEX btyd_params_scope_idx
  ON btyd_params (brand_id, group_id, as_of DESC);

REVOKE UPDATE, DELETE ON btyd_params FROM PUBLIC;

-- migrate:down
DROP TABLE IF EXISTS btyd_params;
