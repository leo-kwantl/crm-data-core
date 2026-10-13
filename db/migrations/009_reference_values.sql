-- migrate:up
-- Reference (master) data: small controlled vocabularies live in ONE table
-- instead of hardcoded enums / per-domain CHECK constraints.
-- Additive: new table + seed rows only. The DROP CONSTRAINTs below remove
-- enum CHECKs (no data change) so reference_values becomes the single source
-- of truth; app-layer validation looks codes up here (see routers/).
-- Seed inserts are ON CONFLICT DO NOTHING (idempotent for test re-runs).

CREATE TABLE IF NOT EXISTS reference_values (
  domain TEXT NOT NULL,
  code TEXT NOT NULL,
  label_en TEXT NOT NULL,
  label_zh TEXT NOT NULL,
  sort_rank INT NOT NULL DEFAULT 0,
  is_active BOOLEAN NOT NULL DEFAULT true,
  PRIMARY KEY (domain, code)
);
CREATE INDEX IF NOT EXISTS reference_values_domain_idx
  ON reference_values (domain, sort_rank, code);

-- Graduate the enum CHECKs to reference lookups (constraint removal only).
ALTER TABLE IF EXISTS consents DROP CONSTRAINT IF EXISTS consents_channel_check;
ALTER TABLE IF EXISTS consents DROP CONSTRAINT IF EXISTS consents_status_check;
ALTER TABLE IF EXISTS campaigns DROP CONSTRAINT IF EXISTS campaigns_status_check;

-- channel: matches the vocab previously hardcoded in schemas/consent.py
-- (sms/email/whatsapp) plus seeds in db/seeds/demo.sql.
INSERT INTO reference_values (domain, code, label_en, label_zh, sort_rank) VALUES
  ('channel', 'sms', 'SMS', '短訊', 10),
  ('channel', 'email', 'Email', '電郵', 20),
  ('channel', 'whatsapp', 'WhatsApp', '即時通訊', 30)
ON CONFLICT DO NOTHING;

-- consent_status: previously Literal["opted_in", "opted_out"].
INSERT INTO reference_values (domain, code, label_en, label_zh, sort_rank) VALUES
  ('consent_status', 'opted_in', 'Opted in', '已同意', 10),
  ('consent_status', 'opted_out', 'Opted out', '已拒絕', 20)
ON CONFLICT DO NOTHING;

-- order_status: orders.status previously free TEXT defaulting to 'completed'
-- (seed uses 'completed' everywhere); full lifecycle seeded for future writes.
INSERT INTO reference_values (domain, code, label_en, label_zh, sort_rank) VALUES
  ('order_status', 'pending', 'Pending', '待處理', 10),
  ('order_status', 'paid', 'Paid', '已付款', 20),
  ('order_status', 'shipped', 'Shipped', '已發貨', 30),
  ('order_status', 'completed', 'Completed', '已完成', 40),
  ('order_status', 'cancelled', 'Cancelled', '已取消', 50)
ON CONFLICT DO NOTHING;

-- staff_role: matches roles used in db/seeds/demo.sql (sa/manager/admin).
INSERT INTO reference_values (domain, code, label_en, label_zh, sort_rank) VALUES
  ('staff_role', 'sa', 'Shop assistant', '店務員', 10),
  ('staff_role', 'manager', 'Manager', '經理', 20),
  ('staff_role', 'admin', 'Admin', '管理員', 30)
ON CONFLICT DO NOTHING;

-- campaign_kind: previously defaulted to 'promotion' with no validation.
INSERT INTO reference_values (domain, code, label_en, label_zh, sort_rank) VALUES
  ('campaign_kind', 'promotion', 'Promotion', '促銷活動', 10)
ON CONFLICT DO NOTHING;

-- campaign_status: the draft -> active -> ended machine is ORDERED by
-- sort_rank (transition = same rank, or next rank up). Do not reorder
-- without updating the transition logic in services/api/repos/campaigns.py.
INSERT INTO reference_values (domain, code, label_en, label_zh, sort_rank) VALUES
  ('campaign_status', 'draft', 'Draft', '草稿', 10),
  ('campaign_status', 'active', 'Active', '進行中', 20),
  ('campaign_status', 'ended', 'Ended', '已結束', 30)
ON CONFLICT DO NOTHING;

-- event_kind: matches kinds seeded in db/seeds/demo.sql
-- (visit/complaint/note/redemption/call); previously free TEXT.
INSERT INTO reference_values (domain, code, label_en, label_zh, sort_rank) VALUES
  ('event_kind', 'visit', 'Store visit', '到店', 10),
  ('event_kind', 'call', 'Phone call', '來電', 20),
  ('event_kind', 'note', 'Note', '備註', 30),
  ('event_kind', 'complaint', 'Complaint', '投訴', 40),
  ('event_kind', 'redemption', 'Redemption', '兌換', 50)
ON CONFLICT DO NOTHING;

-- event_source: matches sources seeded in db/seeds/demo.sql
-- (pos/manual/campaign) plus web for the storefront.
INSERT INTO reference_values (domain, code, label_en, label_zh, sort_rank) VALUES
  ('event_source', 'manual', 'Manual entry', '人手輸入', 10),
  ('event_source', 'pos', 'POS', '銷售終端', 20),
  ('event_source', 'web', 'Website', '網站', 30),
  ('event_source', 'campaign', 'Campaign', '營銷活動', 40)
ON CONFLICT DO NOTHING;

-- migrate:down
DELETE FROM reference_values WHERE domain IN
  ('channel', 'consent_status', 'order_status', 'staff_role',
   'campaign_kind', 'campaign_status', 'event_kind', 'event_source');
DROP TABLE IF EXISTS reference_values;
