-- Identity queries (raw SQL, asyncpg $n placeholders).
-- Account records only: no passwords/sessions anywhere (auth deferred).
-- Lists are newest-first on (created_at DESC, id DESC), keyset-paginated.

-- name: staff_insert
-- params: staff_code text, full_name text, store_id uuid|None, role text
INSERT INTO staff (staff_code, full_name, store_id, role)
VALUES ($1, $2, $3, $4)
RETURNING id, staff_code, full_name, store_id, role, joined_at, left_at, created_at, updated_at;

-- name: staff_by_id
-- params: id uuid
SELECT s.id, s.staff_code, s.full_name, s.store_id, st.code AS store_code,
       s.role, s.joined_at, s.left_at, s.created_at, s.updated_at
FROM staff s LEFT JOIN stores st ON st.id = s.store_id
WHERE s.id = $1;

-- name: staff_list_page
-- params: cursor_at timestamptz|None, cursor_id uuid|None, limit int
SELECT s.id, s.staff_code, s.full_name, s.store_id, st.code AS store_code,
       s.role, s.joined_at, s.left_at, s.created_at, s.updated_at
FROM staff s LEFT JOIN stores st ON st.id = s.store_id
WHERE ($1::timestamptz IS NULL OR (s.created_at, s.id) < ($1::timestamptz, $2::uuid))
ORDER BY s.created_at DESC, s.id DESC
LIMIT $3;

-- name: staff_update
-- params: id uuid, full_name text|None, store_id uuid|None (NULL clears when clear_store), role text|None, left_at timestamptz|None
-- (dynamic SET clause built in the repo; this documents the columns)

-- name: staff_linked_user_count
-- params: staff_id uuid
SELECT COUNT(*) AS n FROM users WHERE staff_id = $1;

-- name: staff_delete
-- params: id uuid
DELETE FROM staff WHERE id = $1;

-- name: user_insert
-- params: username text, display_name text, role text, staff_id uuid|None
INSERT INTO users (username, display_name, role, staff_id)
VALUES ($1, $2, $3, $4)
RETURNING id, username, display_name, role, is_disabled, staff_id, created_at, updated_at;

-- name: user_by_id
-- params: id uuid
SELECT u.id, u.username, u.display_name, u.role, u.is_disabled, u.staff_id,
       s.staff_code, u.created_at, u.updated_at
FROM users u LEFT JOIN staff s ON s.id = u.staff_id
WHERE u.id = $1;

-- name: user_list_page
-- params: cursor_at timestamptz|None, cursor_id uuid|None, limit int
SELECT u.id, u.username, u.display_name, u.role, u.is_disabled, u.staff_id,
       s.staff_code, u.created_at, u.updated_at
FROM users u LEFT JOIN staff s ON s.id = u.staff_id
WHERE ($1::timestamptz IS NULL OR (u.created_at, u.id) < ($1::timestamptz, $2::uuid))
ORDER BY u.created_at DESC, u.id DESC
LIMIT $3;

-- name: unlinked_staff
-- params: (none)
-- Staff rows no account links to (for the user-form staff-link picker).
SELECT id, staff_code, full_name
FROM staff
WHERE id NOT IN (SELECT staff_id FROM users WHERE staff_id IS NOT NULL)
ORDER BY staff_code ASC;
