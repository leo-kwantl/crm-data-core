-- Connector registry + sync-run serving (data D1, data 019).
-- connectors is a GLOBAL registry (no brand/group columns): any signed-in
-- role may list. connector_sync_runs carry scope and use the standard
-- NULL-safe predicate. config holds NON-SECRET params only; secret_ref
-- names an env var — neither is ever expanded here (sanitize-on-read).

-- CONVERGED (app ruling 2026-10-15): has_secret means env-live
-- everywhere (can this connector authenticate right now?). The SELECTs
-- return secret_ref (a NAME, never a value); the routers compute the
-- presence bit Python-side via os.environ. secret_ref IS NOT NULL alone
-- is no longer used as the bit.
-- name: connector_list
SELECT id, source_type, name, config, secret_ref,
       enabled, created_by, created_at, updated_at,
       schedule_cron, last_sync_at, last_sync_status
FROM connectors
ORDER BY source_type, name;

-- name: connector_by_id
-- params: id uuid
SELECT id, source_type, name, config, secret_ref,
       enabled, created_by, created_at, updated_at,
       schedule_cron, last_sync_at, last_sync_status
FROM connectors
WHERE id = $1;

-- name: connector_sync_runs
-- params: connector_id uuid, brand_id uuid, group_id uuid, limit int, entity text|None, status text|None
SELECT id, entity, status, rows_seen, rows_ok, rows_rejected, error,
       started_at, finished_at, created_at
FROM connector_sync_runs
WHERE connector_id = $1
  AND brand_id IS NOT DISTINCT FROM $2
  AND group_id IS NOT DISTINCT FROM $3
  AND ($5::text IS NULL OR entity = $5)
  AND ($6::text IS NULL OR status = $6)
ORDER BY created_at DESC
LIMIT $4;

-- name: connector_last_run
-- params: connector_id uuid, brand_id uuid, group_id uuid
SELECT status, finished_at
FROM connector_sync_runs
WHERE connector_id = $1
  AND brand_id IS NOT DISTINCT FROM $2
  AND group_id IS NOT DISTINCT FROM $3
ORDER BY created_at DESC
LIMIT 1;
