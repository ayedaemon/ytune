-- local_librosa.features was BYTEA but librosa output is a structured dict
-- (tempo, key, mfcc vector, ...) — JSONB fits it and lets Postgres query into
-- it later. No enrich-librosa service has ever run, so this column is empty;
-- dropping it is zero-risk.
ALTER TABLE local_librosa DROP COLUMN IF EXISTS features;
ALTER TABLE local_librosa ADD COLUMN IF NOT EXISTS features JSONB;
