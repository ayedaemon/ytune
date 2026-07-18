-- Same reasoning as 004_librosa_features_jsonb.sql: local_clap.embedding was BYTEA
-- but the embedding is a structured 512-float vector. JSONB fits it, and no service
-- has ever written to this column (0 rows populated) so this is zero-risk.
ALTER TABLE local_clap DROP COLUMN IF EXISTS embedding;
ALTER TABLE local_clap ADD COLUMN IF NOT EXISTS embedding JSONB;
