-- Identity queries (raw SQL, asyncpg $n placeholders).
-- Account records only: no passwords/sessions anywhere (auth deferred).
-- Lists are newest-first on (created_at DESC, id DESC), keyset-paginated.

-- name: staff_insert
-- params: staff_code text, full_name text, store_id uuid|None, role text, brand_id uuid|None
INSERT INTO staff (staff_code, full_name, store_id, role, brand_id)
VALUES ($1, $2, $3, $4, $5)
RETURNING id, staff_code, full_name, store_id, role, brand_id,
  joined_at, left_at, created_at, updated_at;

-- name: staff_by_id
-- params: id uuid, brand_id uuid|None
-- SCOPE: brand_id is the session's own brand (deps.session_brand).
SELECT s.id, s.staff_code, s.full_name, s.store_id, st.code AS store_code,
       s.role, s.brand_id, b.code AS brand_code,
       s.joined_at, s.left_at, s.created_at, s.updated_at
FROM staff s LEFT JOIN stores st ON st.id = s.store_id
LEFT JOIN brands b ON b.id = s.brand_id
WHERE s.id = $1
  AND ($2::uuid IS NULL OR s.brand_id = $2::uuid);

-- name: staff_list_page
-- params: cursor_at timestamptz|None, cursor_id uuid|None, limit int, q text|None, brand_id uuid|None
-- SCOPE: brand_id is the session's own brand (deps.session_brand).
SELECT s.id, s.staff_code, s.full_name, s.store_id, st.code AS store_code, s.role, s.brand_id, b.code AS brand_code, s.joined_at, s.left_at, s.created_at, s.updated_at FROM staff s LEFT JOIN stores st ON st.id = s.store_id LEFT JOIN brands b ON b.id = s.brand_id WHERE ($1::timestamptz IS NULL OR (s.created_at, s.id) < ($1::timestamptz, $2::uuid))   AND ($4::text IS NULL OR (s.staff_code ILIKE '%' || $4 || '%'     OR s.full_name ILIKE '%' || $4 || '%'))   AND ($5::uuid IS NULL OR s.brand_id = $5::uuid) ORDER BY s.created_at DESC, s.id DESC LIMIT $3;

-- name: staff_update
-- params: id uuid, full_name text|None, store_id uuid|None (NULL clears when clear_store), role text|None, left_at timestamptz|None
-- (dynamic SET clause built in the repo; this documents the columns)

-- name: staff_linked_user_count
-- params: staff_id uuid
SELECT COUNT(*) FROM users WHERE staff_id = $1;

-- name: staff_delete
-- params: id uuid
DELETE FROM staff WHERE id = $1;

-- name: user_insert
-- params: username text, display_name text, role text, staff_id uuid|None
INSERT INTO users (username, display_name, role, staff_id)
VALUES ($1, $2, $3, $4)
RETURNING id, username, display_name, role, is_disabled, staff_id, created_at, updated_at;

-- name: user_by_id
-- params: id uuid, brand_id uuid|None
-- SCOPE: brand_id is the session's own brand (deps.session_brand); an account
-- belongs to the brand of the staff member it is linked to.
SELECT u.id, u.username, u.display_name, u.role, u.is_disabled, u.staff_id,
       s.staff_code, u.created_at, u.updated_at, ls.last_sign_in_at
FROM users u LEFT JOIN staff s ON s.id = u.staff_id
LEFT JOIN LATERAL (
  SELECT max(ss.created_at) AS last_sign_in_at FROM sessions ss
  WHERE ss.user_id = u.id
) ls ON TRUE
WHERE u.id = $1
  AND ($2::uuid IS NULL OR s.brand_id = $2::uuid);

-- name: user_list_page
-- params: cursor_at timestamptz|None, cursor_id uuid|None, limit int, q text|None, brand_id uuid|None
-- SCOPE: brand_id is the session's own brand (deps.session_brand); an account
-- belongs to the brand of the staff member it is linked to.
SELECT u.id, u.username, u.display_name, u.role, u.is_disabled, u.staff_id, s.staff_code, u.created_at, u.updated_at, ls.last_sign_in_at FROM users u LEFT JOIN staff s ON s.id = u.staff_id LEFT JOIN LATERAL (   SELECT max(ss.created_at) AS last_sign_in_at FROM sessions ss   WHERE ss.user_id = u.id ) ls ON TRUE WHERE ($1::timestamptz IS NULL OR (u.created_at, u.id) < ($1::timestamptz, $2::uuid))   AND ($4::text IS NULL OR (u.username ILIKE '%' || $4 || '%'     OR u.display_name ILIKE '%' || $4 || '%'))   AND ($5::uuid IS NULL OR s.brand_id = $5::uuid) ORDER BY u.created_at DESC, u.id DESC LIMIT $3;

-- name: unlinked_staff
-- params: brand_id uuid|None
-- Staff rows no account links to (for the user-form staff-link picker).
-- SCOPE: brand_id is the session's own brand (deps.session_brand).
SELECT id, staff_code, full_name
FROM staff
WHERE id NOT IN (SELECT staff_id FROM users WHERE staff_id IS NOT NULL)
  AND ($1::uuid IS NULL OR brand_id = $1::uuid)
ORDER BY staff_code ASC;
