-- migrate:up
-- `date_of_birth <= CURRENT_DATE` was session-dependent.
--
-- CURRENT_DATE renders in the session time zone, and pgembed ships no tzdata
-- but DOES accept a numeric offset (`?options=-c timezone=+08:00`), so the
-- newest acceptable birth date moved with the connection: measured on the dev
-- cluster, the same insert is accepted under one session and rejected under
-- another, one day apart. A birth date is a property of the person, not of the
-- connection that wrote it, so the bound is pinned to the business day (+08
-- wall clock) instead.
--
-- `AT TIME ZONE interval '8 hours'` is the same spelling `pipelines/snapshot.py`
-- uses (`BUSINESS_TZ_SQL`) and for the same reason: named zones need tzdata this
-- build does not have. The expression stays STABLE, which a CHECK may call, and
-- it is monotone in time, so restoring an older dump cannot start failing rows
-- that were valid when written (the bound only ever loosens).
ALTER TABLE customers DROP CONSTRAINT IF EXISTS customers_date_of_birth_check;
ALTER TABLE customers ADD CONSTRAINT customers_date_of_birth_check
  CHECK (date_of_birth IS NULL
         OR date_of_birth <= (now() AT TIME ZONE interval '8 hours')::date);

-- migrate:down
ALTER TABLE customers DROP CONSTRAINT IF EXISTS customers_date_of_birth_check;
ALTER TABLE customers ADD CONSTRAINT customers_date_of_birth_check
  CHECK (date_of_birth IS NULL OR date_of_birth <= CURRENT_DATE);
