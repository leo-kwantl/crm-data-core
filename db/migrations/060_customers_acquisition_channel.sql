-- migrate:up
-- Acquisition channel: where a customer came from (walk-in, online,
-- referral, ...). One attribute on the customer, not per order: the
-- overview's channel donut groups customers by it, and the customer
-- form lets staff record it at creation. Existing rows read 'unknown'
-- until staff set them — honest, not derived.
--
-- The vocabulary lives in reference_values (domain
-- 'acquisition_channel'), the single source of truth per 009; the
-- column default 'unknown' is itself a seeded code so every row joins.

ALTER TABLE IF EXISTS customers
  ADD COLUMN IF NOT EXISTS acquisition_channel text NOT NULL DEFAULT 'unknown';

INSERT INTO reference_values (domain, code, label_en, label_zh, sort_rank) VALUES
  ('acquisition_channel', 'unknown', 'Unknown', '未知', 0),
  ('acquisition_channel', 'walk_in', 'Walk-in', '親臨門市', 10),
  ('acquisition_channel', 'online', 'Online', '網上', 20),
  ('acquisition_channel', 'referral', 'Referral', '親友介紹', 30),
  ('acquisition_channel', 'phone', 'Phone', '電話', 40),
  ('acquisition_channel', 'social', 'Social media', '社交媒體', 50),
  ('acquisition_channel', 'event', 'Event', '活動', 60)
ON CONFLICT DO NOTHING;

-- migrate:down
-- Best-effort: drops the column (row data lost) and the seeded vocab.
ALTER TABLE IF EXISTS customers DROP COLUMN IF EXISTS acquisition_channel;
DELETE FROM reference_values WHERE domain = 'acquisition_channel';
