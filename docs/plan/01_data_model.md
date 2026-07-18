# YTune — Data Model & DB Schema

## Why YouTube IDs as Primary Keys

YTM provides stable, globally unique IDs. Using them directly:
- Eliminates UUID generation
- Makes URL construction trivial (`youtube.com/playlist?list=<id>`)
- Avoids an extra lookup layer between YTM data and local DB rows

## Tables

### `yt_playlists`

```sql
CREATE TYPE playlist_type_enum AS ENUM ('user', 'suggested', 'mood');
CREATE TYPE sync_state_enum   AS ENUM ('pending', 'syncing', 'synced', 'error');

CREATE TABLE yt_playlists (
    id                     TEXT PRIMARY KEY,        -- actual YT playlist ID
    title                  TEXT NOT NULL,
    owner                  TEXT NOT NULL,           -- channel id or user identifier
    playlist_type          playlist_type_enum NOT NULL DEFAULT 'user',
    song_count             INT,
    sync_state             sync_state_enum NOT NULL DEFAULT 'pending',
    last_synced_at         TIMESTAMPTZ,
    candidate_playlist_ids JSONB DEFAULT '[]',      -- [{id, title}] from related
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_yt_playlists_type       ON yt_playlists(playlist_type);
CREATE INDEX idx_yt_playlists_sync_state ON yt_playlists(sync_state);
CREATE INDEX idx_yt_playlists_synced_at  ON yt_playlists(last_synced_at);
```

### `yt_tracks`

```sql
CREATE TYPE track_type_enum AS ENUM ('user', 'suggested');

CREATE TABLE yt_tracks (
    id                 TEXT PRIMARY KEY,            -- actual YT video ID
    title              TEXT,
    artist             TEXT,
    album              TEXT,
    duration_seconds   INT,
    track_type         track_type_enum NOT NULL DEFAULT 'user',
    source_playlist_id TEXT REFERENCES yt_playlists(id) ON DELETE SET NULL,
    metadata_json      JSONB DEFAULT '{}',          -- full raw ytmusicapi payload
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_yt_tracks_playlist ON yt_tracks(source_playlist_id);
CREATE INDEX idx_yt_tracks_type     ON yt_tracks(track_type);
```

### `track_downloaded`

```sql
CREATE TYPE download_status_enum AS ENUM ('pending', 'downloading', 'done', 'error');

CREATE TABLE track_downloaded (
    id            SERIAL PRIMARY KEY,
    video_id      TEXT UNIQUE NOT NULL REFERENCES yt_tracks(id) ON DELETE CASCADE,
    file_path     TEXT,
    status        download_status_enum NOT NULL DEFAULT 'pending',
    error_message TEXT,
    retries       INT NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_track_downloaded_status   ON track_downloaded(status);
CREATE INDEX idx_track_downloaded_video_id ON track_downloaded(video_id);
```

### `local_librosa`, `local_clap`, `local_mert`

All three share the same structure. `local_librosa` uses `features JSONB`; `local_clap` and
`local_mert` use `embedding JSONB` (rename column).

```sql
CREATE TYPE enrichment_status_enum AS ENUM ('pending', 'processing', 'done', 'error');

CREATE TABLE local_librosa (
    id            SERIAL PRIMARY KEY,
    file_path     TEXT UNIQUE NOT NULL,
    video_id      TEXT REFERENCES yt_tracks(id) ON DELETE SET NULL,
    features      JSONB,
    status        enrichment_status_enum NOT NULL DEFAULT 'pending',
    error_message TEXT,
    retries       INT NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_local_librosa_status   ON local_librosa(status);
CREATE INDEX idx_local_librosa_video_id ON local_librosa(video_id);
CREATE INDEX idx_local_librosa_path     ON local_librosa(file_path);

-- local_clap and local_mert: identical structure, rename `features` to `embedding`
CREATE TABLE local_clap (LIKE local_librosa INCLUDING ALL);
ALTER TABLE local_clap RENAME COLUMN features TO embedding;

CREATE TABLE local_mert (LIKE local_librosa INCLUDING ALL);
ALTER TABLE local_mert RENAME COLUMN features TO embedding;
```

> **Embedding size note**: CLAP = 512 floats (~2KB JSONB). MERT = 768 floats (~3KB JSONB).
> For cosine similarity queries at scale, migrate to `pgvector` with `vector(512)` column type
> and an `ivfflat` index. Blueprint defaults to JSONB for zero-dep simplicity.

### `sync_jobs`

```sql
CREATE TYPE job_status_enum AS ENUM ('queued', 'running', 'done', 'error');

CREATE TABLE sync_jobs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status      job_status_enum NOT NULL DEFAULT 'queued',
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error       TEXT,
    stats       JSONB DEFAULT '{}',  -- playlists_synced, tracks_synced, etc.
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### `mood_playlists` and `mood_track_map`

```sql
CREATE TABLE mood_playlists (
    id          SERIAL PRIMARY KEY,
    label       TEXT NOT NULL,      -- e.g., "energetic", "melancholic"
    cluster_id  INT,
    track_count INT DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE mood_track_map (
    mood_playlist_id INT REFERENCES mood_playlists(id) ON DELETE CASCADE,
    video_id         TEXT REFERENCES yt_tracks(id) ON DELETE CASCADE,
    score            FLOAT,
    PRIMARY KEY (mood_playlist_id, video_id)
);
```

## ER Diagram

```text
yt_playlists (id PK)
    │
    │ 1:N
    ▼
yt_tracks (id PK, source_playlist_id FK)
    │
    │ 1:1
    ▼
track_downloaded (video_id UNIQUE FK)
    │
    │ file_path (shared key to local_* tables)
    ├──────────────────────────────────┐────────────────────────┐
    ▼                                  ▼                        ▼
local_librosa (file_path UNIQUE)   local_clap            local_mert

yt_tracks ──── mood_track_map ──── mood_playlists
```

## Full Enrichment State Query

```sql
SELECT
    t.id,
    t.title,
    t.artist,
    td.status         AS download_status,
    td.file_path,
    ll.status         AS librosa_status,
    lc.status         AS clap_status,
    lm.status         AS mert_status
FROM yt_tracks t
LEFT JOIN track_downloaded  td ON td.video_id = t.id
LEFT JOIN local_librosa     ll ON ll.video_id = t.id
LEFT JOIN local_clap        lc ON lc.video_id = t.id
LEFT JOIN local_mert        lm ON lm.video_id = t.id
WHERE t.id = $1;
```

## Idempotency & Retry Tracking

- All upserts use `INSERT ... ON CONFLICT DO UPDATE SET updated_at = NOW()`.
- `retries` column incremented before each retry attempt.
- `status = 'processing'` claimed with `FOR UPDATE SKIP LOCKED`.
- Workers set status back to `pending` on retriable failure, `error` on exhaustion.
- Stuck `processing` rows recovered at worker startup (see `00_overview.md`).
