-- Demo seed (never mixed with real rows). Idempotent via fixed UUIDs.
-- 8 customers (one optical w/ attributes.eye), contacts, consents (one all-opted-out),
-- 2 stores, 12 products, 15 orders w/ items spanning ~6 months.

-- Stores (brand_id explicit: migration 011 made it NOT NULL, and the DEMO
-- brand row is inserted by the migration before any seed runs).
INSERT INTO stores (id, code, name, brand_id) VALUES
  ('11111111-1111-1111-1111-111111111111', 'CWB-01', 'Causeway Bay Flagship',
   '60100000-0000-0000-0000-000000000001'),
  ('22222222-2222-2222-2222-222222222222', 'TSM-02', 'Tsim Sha Tsui',
   '60100000-0000-0000-0000-000000000001')
ON CONFLICT (id) DO NOTHING;

-- Products (12)
INSERT INTO products (id, sku, name, category, price_cents) VALUES
  ('a0000000-0000-0000-0000-000000000001', 'FRM-001', 'Classic Acetate Frame', 'frames', 12900),
  ('a0000000-0000-0000-0000-000000000002', 'FRM-002', 'Titanium Rimless Frame', 'frames', 24900),
  ('a0000000-0000-0000-0000-000000000003', 'LENS-001', '1.67 High-Index Lenses', 'lenses', 9900),
  ('a0000000-0000-0000-0000-000000000004', 'LENS-002', 'Blue-Light Filter Lenses', 'lenses', 7900),
  ('a0000000-0000-0000-0000-000000000005', 'SUN-001', 'Polarized Sunglasses', 'sunglasses', 15900),
  ('a0000000-0000-0000-0000-000000000006', 'SUN-002', 'Sport Wrap Sunglasses', 'sunglasses', 13900),
  ('a0000000-0000-0000-0000-000000000007', 'ACC-001', 'Leather Hard Case', 'accessories', 3900),
  ('a0000000-0000-0000-0000-000000000008', 'ACC-002', 'Microfiber Cloth 3-pack', 'accessories', 1200),
  ('a0000000-0000-0000-0000-000000000009', 'ACC-003', 'Anti-Fog Spray', 'accessories', 1500),
  ('a0000000-0000-0000-0000-000000000010', 'FRM-003', 'Round Vintage Frame', 'frames', 14900),
  ('a0000000-0000-0000-0000-000000000011', 'LENS-003', 'Progressive Lenses', 'lenses', 19900),
  ('a0000000-0000-0000-0000-000000000012', 'CON-001', 'Contact Lens Solution', 'care', 1800)
ON CONFLICT (id) DO NOTHING;

-- Product enrichment (migration 008 merchandising columns). UPDATE-guarded so
-- re-seeds never clobber operator edits; only fills rows still on defaults.
UPDATE products SET
  description = 'Hand-polished acetate frame with a timeless rectangular silhouette. Hypoallergenic hinges, suitable for single-vision and progressive lenses.',
  specs = '{"lens_width": 52, "bridge": 20, "temple": 145, "color": "tortoise", "material": "acetate"}',
  image_url = 'https://picsum.photos/seed/frm-001/600/400'
WHERE sku = 'FRM-001' AND description IS NULL;
UPDATE products SET
  description = 'Featherweight titanium rimless frame for all-day comfort. Adjustable nose pads, ideal for strong prescriptions seeking a minimal look.',
  specs = '{"lens_width": 54, "bridge": 18, "temple": 140, "color": "gunmetal", "material": "titanium"}',
  image_url = 'https://picsum.photos/seed/frm-002/600/400'
WHERE sku = 'FRM-002' AND description IS NULL;
UPDATE products SET
  description = 'Polarized sunglasses with full UV400 protection. Reduces road and water glare; includes hard case and microfiber cloth.',
  specs = '{"lens_width": 58, "bridge": 16, "temple": 135, "color": "matte black", "material": "TR90", "polarized": true}',
  image_url = 'https://picsum.photos/seed/sun-001/600/400'
WHERE sku = 'SUN-001' AND description IS NULL;

-- Per-store stock for the enriched products (+ case top-up). Idempotent pairs.
INSERT INTO inventory (product_id, store_id, qty)
SELECT p.id, s.id, v.qty FROM (VALUES
  ('FRM-001', 'CWB-01', 12), ('FRM-001', 'TSM-02', 7),
  ('FRM-002', 'CWB-01', 5),  ('FRM-002', 'TSM-02', 3),
  ('SUN-001', 'CWB-01', 9),  ('SUN-001', 'TSM-02', 11),
  ('ACC-001', 'CWB-01', 20)
) AS v(sku, code, qty)
JOIN products p ON p.sku = v.sku JOIN stores s ON s.code = v.code
ON CONFLICT (product_id, store_id) DO NOTHING;

-- Customers (8)
INSERT INTO customers (id, member_id, full_name, attributes,
  gender, date_of_birth, district, address_line) VALUES
  ('c0000000-0000-0000-0000-000000000001', 'M-000101', 'Chan Tai Man', '{"locale": "zh-HK", "eye": {"od_sph": "-2.50", "os_sph": "-2.25", "pd": "62"}}',
    'male', '1985-03-14', 'Sham Shui Po', 'Flat 12B, 88 Cheung Sha Wan Road'),
  ('c0000000-0000-0000-0000-000000000002', 'M-000102', 'Wong Mei Ling', '{"locale": "zh-HK"}',
    'female', '1990-07-22', 'Kwun Tong', 'Room 1503, 23 Hoi Yuen Road'),
  ('c0000000-0000-0000-0000-000000000003', 'M-000103', 'Lee Kwok Wai', '{}',
    'male', '1978-11-05', 'Yuen Long', 'House 7, 45 Castle Peak Road'),
  ('c0000000-0000-0000-0000-000000000004', 'M-000104', 'Chan Tai Ming', '{"locale": "zh-HK"}',
    'male', '1998-01-30', 'Sha Tin', 'Flat 8A, 12 Tai Chung Kiu Road'),
  ('c0000000-0000-0000-0000-000000000005', 'M-000105', 'Sarah Chen', '{"locale": "en"}',
    'female', '1992-05-17', 'Central & Western', 'Flat 30C, 9 Queen''s Road Central'),
  ('c0000000-0000-0000-0000-000000000006', 'M-000106', 'David Lau', '{}',
    'male', '1988-09-09', 'Kowloon City', 'Flat 5D, 77 Prince Edward Road West'),
  ('c0000000-0000-0000-0000-000000000007', 'M-000107', 'Emily Ng', '{}',
    'female', '1995-12-03', 'Wan Chai', 'Flat 21F, 6 Stewart Road'),
  ('c0000000-0000-0000-0000-000000000008', 'M-000108', 'Peter Ho', '{}',
    'male', '1975-06-25', 'Eastern', 'Flat 3G, 100 Shau Kei Wan Road')
ON CONFLICT (id) DO NOTHING;

-- Contacts (normalized: E.164 phones, lowercase emails)
INSERT INTO customer_contacts (customer_id, kind, value_normalized, value_display, is_primary) VALUES
  ('c0000000-0000-0000-0000-000000000001', 'phone', '+85291234567', '+852 9123 4567', true),
  ('c0000000-0000-0000-0000-000000000001', 'email', 'taiman.chan@example.com', 'taiman.chan@example.com', false),
  ('c0000000-0000-0000-0000-000000000002', 'phone', '+85292345678', '+852 9234 5678', true),
  ('c0000000-0000-0000-0000-000000000003', 'email', 'kwokwai.lee@example.com', 'kwokwai.lee@example.com', true),
  ('c0000000-0000-0000-0000-000000000004', 'phone', '+85293456789', '+852 9345 6789', true),
  ('c0000000-0000-0000-0000-000000000005', 'email', 'sarah.chen@example.com', 'sarah.chen@example.com', true),
  ('c0000000-0000-0000-0000-000000000005', 'phone', '+85294567890', '+852 9456 7890', false),
  ('c0000000-0000-0000-0000-000000000006', 'phone', '+85295678901', '+852 9567 8901', true),
  ('c0000000-0000-0000-0000-000000000007', 'email', 'emily.ng@example.com', 'emily.ng@example.com', true),
  ('c0000000-0000-0000-0000-000000000008', 'phone', '+85296789012', '+852 9678 9012', true)
ON CONFLICT DO NOTHING;

-- Consents: mostly opted-in; M-000106 (David Lau) is all-opted-out
INSERT INTO consents (customer_id, channel, status, captured_by) VALUES
  ('c0000000-0000-0000-0000-000000000001', 'sms', 'opted_in', 'seed'),
  ('c0000000-0000-0000-0000-000000000001', 'email', 'opted_in', 'seed'),
  ('c0000000-0000-0000-0000-000000000001', 'whatsapp', 'opted_out', 'seed'),
  ('c0000000-0000-0000-0000-000000000002', 'sms', 'opted_in', 'seed'),
  ('c0000000-0000-0000-0000-000000000002', 'email', 'opted_in', 'seed'),
  ('c0000000-0000-0000-0000-000000000003', 'sms', 'opted_out', 'seed'),
  ('c0000000-0000-0000-0000-000000000003', 'email', 'opted_in', 'seed'),
  ('c0000000-0000-0000-0000-000000000004', 'sms', 'opted_in', 'seed'),
  ('c0000000-0000-0000-0000-000000000004', 'email', 'opted_in', 'seed'),
  ('c0000000-0000-0000-0000-000000000005', 'sms', 'opted_in', 'seed'),
  ('c0000000-0000-0000-0000-000000000005', 'email', 'opted_in', 'seed'),
  ('c0000000-0000-0000-0000-000000000006', 'sms', 'opted_out', 'seed'),
  ('c0000000-0000-0000-0000-000000000006', 'email', 'opted_out', 'seed'),
  ('c0000000-0000-0000-0000-000000000006', 'whatsapp', 'opted_out', 'seed'),
  ('c0000000-0000-0000-0000-000000000007', 'sms', 'opted_in', 'seed'),
  ('c0000000-0000-0000-0000-000000000007', 'email', 'opted_out', 'seed'),
  ('c0000000-0000-0000-0000-000000000008', 'sms', 'opted_in', 'seed'),
  ('c0000000-0000-0000-0000-000000000008', 'email', 'opted_in', 'seed')
ON CONFLICT DO NOTHING;

INSERT INTO consent_events (customer_id, channel, status, captured_by)
SELECT customer_id, channel, status, captured_by FROM consents
ON CONFLICT DO NOTHING;

-- Client POS sync: 2 demo customers carry external ids (editable via API).
UPDATE customers SET external_member_id = 'POS-1001'
WHERE member_id = 'M-000101' AND external_member_id IS NULL;
UPDATE customers SET external_member_id = 'POS-1002'
WHERE member_id = 'M-000102' AND external_member_id IS NULL;

-- Orders (15 across ~6 months) + items
-- Stable keys: a re-seed hits the partial unique index and is a no-op.
-- Backfill first, for a cluster seeded with the file BEFORE the stable keys:
-- those 15 orders carry source/external_order_id NULL, the partial index's
-- predicate is `source IS NOT NULL`, so nothing would conflict and the
-- insert below would ADD 15 more demo orders (the demo set doubles).
-- Matched on customer + amount, which is unique inside the demo set, and
-- only on rows that carry no source at all: the re-seed keeps the demo
-- orders it finds instead of replacing them.
-- Only the EARLIEST row per key is keyed: a cluster whose legacy seed ran
-- twice holds 30 demo orders (the legacy seed was not re-seed safe), and
-- keying all of them would violate orders_source_ext_ux — the seed would
-- fail on exactly the cluster it is meant to repair. The later copies keep
-- no key, so the insert below conflicts with the keyed row and adds
-- nothing; they are the pre-existing duplicates, and a purge is a separate
-- explicit operation.
WITH demo_keys (customer_id, total_cents, external_order_id) AS (VALUES
    ('c0000000-0000-0000-0000-000000000001', 22800, 'DEMO-O-001'),
    ('c0000000-0000-0000-0000-000000000001', 15900, 'DEMO-O-002'),
    ('c0000000-0000-0000-0000-000000000001',  3900, 'DEMO-O-003'),
    ('c0000000-0000-0000-0000-000000000002', 14900, 'DEMO-O-004'),
    ('c0000000-0000-0000-0000-000000000002', 13900, 'DEMO-O-005'),
    ('c0000000-0000-0000-0000-000000000003', 24900, 'DEMO-O-006'),
    ('c0000000-0000-0000-0000-000000000004', 15900, 'DEMO-O-007'),
    ('c0000000-0000-0000-0000-000000000004',  1200, 'DEMO-O-008'),
    ('c0000000-0000-0000-0000-000000000005', 34800, 'DEMO-O-009'),
    ('c0000000-0000-0000-0000-000000000005',  7900, 'DEMO-O-010'),
    ('c0000000-0000-0000-0000-000000000005',  1800, 'DEMO-O-011'),
    ('c0000000-0000-0000-0000-000000000006', 12900, 'DEMO-O-012'),
    ('c0000000-0000-0000-0000-000000000007', 19900, 'DEMO-O-013'),
    ('c0000000-0000-0000-0000-000000000007',  1500, 'DEMO-O-014'),
    ('c0000000-0000-0000-0000-000000000008', 13900, 'DEMO-O-015')
  ),
candidates AS (
    SELECT o.id AS order_id, k.external_order_id,
           row_number() OVER (
               PARTITION BY k.customer_id, k.total_cents
               ORDER BY o.ordered_at, o.id) AS rn
    FROM orders o
    JOIN demo_keys k
      ON o.customer_id = k.customer_id::uuid
     AND o.total_cents = k.total_cents
    WHERE o.source IS NULL
      AND o.status = 'completed'
  )
UPDATE orders o
SET source = 'demo', external_order_id = c.external_order_id
FROM candidates c
WHERE o.id = c.order_id
  AND c.rn = 1;

INSERT INTO orders (customer_id, store_id, ordered_at, total_cents,
                    status, source, external_order_id) VALUES
  ('c0000000-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111', now() - interval '175 days', 22800, 'completed', 'demo', 'DEMO-O-001'),
  ('c0000000-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111', now() - interval '40 days', 15900, 'completed', 'demo', 'DEMO-O-002'),
  ('c0000000-0000-0000-0000-000000000001', '22222222-2222-2222-2222-222222222222', now() - interval '5 days', 3900, 'completed', 'demo', 'DEMO-O-003'),
  ('c0000000-0000-0000-0000-000000000002', '11111111-1111-1111-1111-111111111111', now() - interval '160 days', 14900, 'completed', 'demo', 'DEMO-O-004'),
  ('c0000000-0000-0000-0000-000000000002', '22222222-2222-2222-2222-222222222222', now() - interval '60 days', 13900, 'completed', 'demo', 'DEMO-O-005'),
  ('c0000000-0000-0000-0000-000000000003', '11111111-1111-1111-1111-111111111111', now() - interval '140 days', 24900, 'completed', 'demo', 'DEMO-O-006'),
  ('c0000000-0000-0000-0000-000000000004', '22222222-2222-2222-2222-222222222222', now() - interval '120 days', 15900, 'completed', 'demo', 'DEMO-O-007'),
  ('c0000000-0000-0000-0000-000000000004', '11111111-1111-1111-1111-111111111111', now() - interval '20 days', 1200, 'completed', 'demo', 'DEMO-O-008'),
  ('c0000000-0000-0000-0000-000000000005', '11111111-1111-1111-1111-111111111111', now() - interval '100 days', 34800, 'completed', 'demo', 'DEMO-O-009'),
  ('c0000000-0000-0000-0000-000000000005', '22222222-2222-2222-2222-222222222222', now() - interval '70 days', 7900, 'completed', 'demo', 'DEMO-O-010'),
  ('c0000000-0000-0000-0000-000000000005', '11111111-1111-1111-1111-111111111111', now() - interval '10 days', 1800, 'completed', 'demo', 'DEMO-O-011'),
  ('c0000000-0000-0000-0000-000000000006', '22222222-2222-2222-2222-222222222222', now() - interval '90 days', 12900, 'completed', 'demo', 'DEMO-O-012'),
  ('c0000000-0000-0000-0000-000000000007', '11111111-1111-1111-1111-111111111111', now() - interval '80 days', 19900, 'completed', 'demo', 'DEMO-O-013'),
  ('c0000000-0000-0000-0000-000000000007', '11111111-1111-1111-1111-111111111111', now() - interval '15 days', 1500, 'completed', 'demo', 'DEMO-O-014'),
  ('c0000000-0000-0000-0000-000000000008', '22222222-2222-2222-2222-222222222222', now() - interval '50 days', 13900, 'completed', 'demo', 'DEMO-O-015')
ON CONFLICT DO NOTHING;

-- One or two items per order, totals kept consistent-ish with lines
INSERT INTO order_items (order_id, product_id, qty, unit_price_cents)
SELECT o.id, p.id, 1, p.price_cents
FROM orders o JOIN products p ON p.sku = (
  CASE
    WHEN o.ordered_at < now() - interval '150 days' THEN 'FRM-001'
    WHEN o.total_cents > 20000 THEN 'FRM-002'
    WHEN o.total_cents > 15000 THEN 'SUN-001'
    WHEN o.total_cents > 10000 THEN 'FRM-003'
    WHEN o.total_cents > 5000 THEN 'LENS-002'
    WHEN o.total_cents > 2000 THEN 'ACC-001'
    ELSE 'ACC-002'
  END)
WHERE o.source = 'demo' AND o.external_order_id LIKE 'DEMO-O-%'
  AND NOT EXISTS (SELECT 1 FROM order_items oi WHERE oi.order_id = o.id);

-- Identity seed (account records only — no passwords/sessions; auth deferred).
-- 3 staff (S-014 + S-015 linked to login accounts, S-016 unlinked) +
-- 3 users (2 linked, 1 unlinked HQ account).
INSERT INTO staff (id, staff_code, full_name, store_id, role) VALUES
  ('d0000000-0000-0000-0000-000000000014', 'S-014', 'Chan Siu Ming', '11111111-1111-1111-1111-111111111111', 'sa'),
  ('d0000000-0000-0000-0000-000000000015', 'S-015', 'Wong Ka Yan', '22222222-2222-2222-2222-222222222222', 'sa'),
  ('d0000000-0000-0000-0000-000000000016', 'S-016', 'Lau Chi Keung', NULL, 'manager')
ON CONFLICT (id) DO NOTHING;

INSERT INTO users (id, username, display_name, role, staff_id) VALUES
  ('e0000000-0000-0000-0000-000000000014', 'siu.ming', 'Chan Siu Ming', 'sa', 'd0000000-0000-0000-0000-000000000014'),
  ('e0000000-0000-0000-0000-000000000015', 'ka.yan', 'Wong Ka Yan', 'sa', 'd0000000-0000-0000-0000-000000000015'),
  ('e0000000-0000-0000-0000-000000000099', 'hq.admin', 'HQ Admin', 'admin', NULL)
ON CONFLICT (id) DO NOTHING;

-- Membership tiers (manual assignment; no points engine in MVP) + 3 demo assignments.
INSERT INTO membership_tiers (code, name, rank, benefits_text) VALUES
  ('SILVER', 'Silver', 1, '5% off accessories, birthday voucher'),
  ('GOLD', 'Gold', 2, '10% off frames, free lens cloth quarterly'),
  ('PLATINUM', 'Platinum', 3, '15% off everything, priority eye exams')
ON CONFLICT (code) DO NOTHING;

UPDATE customers SET tier_code = 'GOLD'
WHERE member_id = 'M-000101' AND tier_code IS NULL;
UPDATE customers SET tier_code = 'SILVER'
WHERE member_id = 'M-000102' AND tier_code IS NULL;
UPDATE customers SET tier_code = 'PLATINUM'
WHERE member_id = 'M-000105' AND tier_code IS NULL;

-- Demo campaigns: one active (dashboard counts it), one draft.
INSERT INTO campaigns (id, code, name, kind, status, config) VALUES
  ('f0000000-0000-0000-0000-000000000001', 'MID-AUTUMN-10', 'Mid-Autumn 10% Off Frames', 'promotion', 'active', '{"discount_pct": 10}'),
  ('f0000000-0000-0000-0000-000000000002', 'WINTER-VIP-PREVIEW', 'Winter VIP Preview', 'promotion', 'draft', '{"discount_pct": 15}')
ON CONFLICT (id) DO NOTHING;

INSERT INTO campaign_members (campaign_id, customer_id, assigned_by)
SELECT 'f0000000-0000-0000-0000-000000000001', id, 'seed'
FROM customers WHERE member_id IN ('M-000101', 'M-000105')
ON CONFLICT DO NOTHING;

-- Customer journey events (append-only timeline; kinds are free TEXT — the
-- vocabulary is owned by a later reference-data pass, no lookup table).
-- M-000101 (Chan Tai Man) x3, M-000105 (Sarah Chen) x2.
INSERT INTO customer_events (id, customer_id, occurred_at, kind, source, summary, payload, created_by) VALUES
  ('b0000000-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000001', now() - INTERVAL '9 days', 'visit', 'pos',
   'Store visit - frame fitting at Central flagship',
   '{"store_code": "CEN-01", "staff": "S-014"}', 'S-014'),
  ('b0000000-0000-0000-0000-000000000002', 'c0000000-0000-0000-0000-000000000001', now() - INTERVAL '6 days', 'complaint', 'manual',
   'Complaint: loose temple arm on new frames - resolved with free repair',
   '{"resolution": "free_repair", "status": "resolved"}', 'S-014'),
  ('b0000000-0000-0000-0000-000000000003', 'c0000000-0000-0000-0000-000000000001', now() - INTERVAL '2 days', 'note', 'manual',
   'Manual note: prefers contact via WhatsApp after 6pm',
   '{"preference": "whatsapp_after_18"}', 'S-015'),
  ('b0000000-0000-0000-0000-000000000004', 'c0000000-0000-0000-0000-000000000005', now() - INTERVAL '4 days', 'redemption', 'campaign',
   'Campaign redemption: Mid-Autumn 10% Off Frames applied at checkout',
   '{"campaign_code": "MID-AUTUMN-10"}', 'S-015'),
  ('b0000000-0000-0000-0000-000000000005', 'c0000000-0000-0000-0000-000000000005', now() - INTERVAL '1 day', 'call', 'manual',
   'Follow-up call: satisfied with redemption, interested in Winter VIP Preview',
   '{"outcome": "interested", "campaign_code": "WINTER-VIP-PREVIEW"}', 'S-015')
ON CONFLICT (id) DO NOTHING;

-- Demo login passwords (bcrypt; documented for the pilot walk-through —
-- these are throwaway demo creds, admins must reset them via
-- POST /users/:id/password before any real use).
-- siu.ming / ka.yan : changeme-<username> (linked to S-014 / S-015, role sa)
-- hq.admin          : changeme-hq.admin (no staff link, role admin)
UPDATE users SET password_hash = '$2b$12$oAcUDVOPzqn8wVyK9y/MPe1SHqTYycZpNqGu/OB495GqZ/vRYPdaS'
WHERE username = 'siu.ming' AND password_hash IS NULL;
UPDATE users SET password_hash = '$2b$12$z512la1j.OGltoEfDly4Muwo5vsXDwzW3gIE7GiuzfklPtsdEFM.O'
WHERE username = 'ka.yan' AND password_hash IS NULL;
UPDATE users SET password_hash = '$2b$12$cwy8wcIWh2ky5m6SVSVmfO8v4fU8NWw3QxwtUQ/He7Uz4wHyL40Bq'
WHERE username = 'hq.admin' AND password_hash IS NULL;

-- Multi-brand hierarchy (migration 011): default DEMO group + brand housing
-- the 2 demo stores, then backfill of all seed rows. UPDATE-guarded so
-- re-seeds never clobber operator edits; only fills rows still unscoped.
-- Canonical counts stay stable: exactly one group + one brand added.
INSERT INTO groups (id, code, name) VALUES
  ('60000000-0000-0000-0000-000000000001', 'DEMO', 'Demo Group')
ON CONFLICT (id) DO NOTHING;

INSERT INTO brands (id, group_id, code, name) VALUES
  ('60100000-0000-0000-0000-000000000001',
   '60000000-0000-0000-0000-000000000001', 'DEMO', 'Demo Flagship Brand')
ON CONFLICT (id) DO NOTHING;

UPDATE stores SET brand_id = '60100000-0000-0000-0000-000000000001'
WHERE brand_id IS NULL;
UPDATE staff SET brand_id = stores.brand_id
FROM stores WHERE stores.id = staff.store_id
  AND staff.brand_id IS NULL AND staff.store_id IS NOT NULL;
-- S-016 (manager, no home store) intentionally keeps brand_id NULL =
-- group-wide, per the access-model convention.
UPDATE campaigns SET brand_id = '60100000-0000-0000-0000-000000000001'
WHERE brand_id IS NULL;
UPDATE customers SET group_id = '60000000-0000-0000-0000-000000000001'
WHERE group_id IS NULL;
-- linked_customer_id stays NULL for every seed row: no auto-merge/matching
-- logic exists; linkage is an explicit future flow (pure reservation).
UPDATE consents SET brand_id = '60100000-0000-0000-0000-000000000001'
WHERE brand_id IS NULL;
