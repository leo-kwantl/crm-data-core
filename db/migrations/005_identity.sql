-- migrate:up
-- Identity module: ACCOUNT RECORDS ONLY — staff roster + login-account rows.
-- There are NO password/secret/session columns here by design. Real
-- login/auth is deferred to pilot hardening; the API keeps the x-staff-id
-- stub header until then (see services/api/deps.py get_staff_id).
CREATE TABLE staff (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  staff_code text NOT NULL UNIQUE, -- e.g. S-014 (matches the x-staff-id stub values)
  full_name text NOT NULL CHECK (char_length(full_name) BETWEEN 1 AND 200),
  store_id uuid NULL REFERENCES stores (id),
  role text NOT NULL DEFAULT 'sa',
  joined_at timestamptz NOT NULL DEFAULT now(),
  left_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  username text NOT NULL UNIQUE,
  display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 200),
  role text NOT NULL DEFAULT 'sa', -- e.g. admin/manager/sa
  is_disabled boolean NOT NULL DEFAULT false,
  staff_id uuid NULL REFERENCES staff (id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN users.staff_id IS 'nullable link to staff roster; a login account is sometimes not frontline staff';
CREATE INDEX IF NOT EXISTS users_staff_id_idx ON users (staff_id) WHERE staff_id IS NOT NULL;

-- migrate:down
DROP INDEX IF EXISTS users_staff_id_idx;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS staff;
