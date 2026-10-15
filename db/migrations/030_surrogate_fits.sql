-- migrate:up
-- 030: LM1 surrogate-fit registry + explain method labels (data,
-- [ADDITIVE] analytics, joint w/ ml).
--
-- Per the reviewed LM1 method spec: one fit row per segment run
-- (XGBClassifier on segmentation inputs -> cluster labels, fidelity
-- gate accuracy >= 0.90 or method 'surrogate_unfitted' with NO shap
-- rows). Accuracy lives HERE, on the fit row only. explain_batches
-- gains fit_id (NULL for mean-method rows); batches + rows gain a
-- method label (default 'segment_mean' = all history before this
-- migration, zero backfill rewrite). No tick writes under
-- 'xgb_surrogate_v1' until the LM1 spec ACK (given with this
-- migration) — the gate is contractual, enforced writer-side.

CREATE TABLE seg_surrogate_fits (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  segment_run_id uuid NOT NULL UNIQUE
    REFERENCES segment_runs (id) ON DELETE CASCADE,
  trained_at timestamptz NOT NULL DEFAULT now(),
  accuracy double precision NOT NULL
    CHECK (accuracy >= 0 AND accuracy <= 1),
  feature_names jsonb NOT NULL DEFAULT '[]'::jsonb,
  xgb_params jsonb NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE explain_batches
  ADD COLUMN method text NOT NULL DEFAULT 'segment_mean'
    CHECK (char_length(method) BETWEEN 1 AND 64),
  ADD COLUMN fit_id uuid NULL
    REFERENCES seg_surrogate_fits (id) ON DELETE SET NULL;

ALTER TABLE explain_rows
  ADD COLUMN method text NOT NULL DEFAULT 'segment_mean'
    CHECK (char_length(method) BETWEEN 1 AND 64);

REVOKE UPDATE, DELETE ON seg_surrogate_fits FROM PUBLIC;

-- migrate:down
ALTER TABLE explain_rows DROP COLUMN IF EXISTS method;
ALTER TABLE explain_batches DROP COLUMN IF EXISTS fit_id;
ALTER TABLE explain_batches DROP COLUMN IF EXISTS method;
DROP TABLE IF EXISTS seg_surrogate_fits;
