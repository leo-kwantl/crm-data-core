-- migrate:up
-- Real password auth (replaces the x-staff-id stub; see services/api/deps.py).
-- Admin-provisioned accounts: no self-signup, no SSO (parked).
-- users.password_hash NULL = login disabled until an admin sets a password.
-- sessions stores SHA-256 hex of the bearer token only (never the token).
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT NULL;

CREATE TABLE IF NOT EXISTS sessions (
  token_sha256 TEXT PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  staff_code TEXT NOT NULL,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions (user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_at_idx ON sessions (expires_at);

-- migrate:down
DROP INDEX IF EXISTS sessions_expires_at_idx;
DROP INDEX IF EXISTS sessions_user_id_idx;
DROP TABLE IF EXISTS sessions;
ALTER TABLE users DROP COLUMN IF EXISTS password_hash;
