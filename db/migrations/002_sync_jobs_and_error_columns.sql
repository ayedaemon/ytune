-- sync_jobs: tracks API-initiated sync requests
CREATE TABLE IF NOT EXISTS sync_jobs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status      TEXT CHECK (status IN ('queued', 'running', 'done', 'error')) NOT NULL DEFAULT 'queued',
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error       TEXT,
    stats       JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sync_jobs_status ON sync_jobs(status);

-- error_message columns for enrichment tables
ALTER TABLE track_downloaded ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE local_clap       ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE local_mert       ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE local_librosa    ADD COLUMN IF NOT EXISTS error_message TEXT;
