-- 1. Core Metadata Tables
CREATE TABLE yt_playlists (
    id TEXT PRIMARY KEY,
    title TEXT,
    owner TEXT,
    playlist_type TEXT CHECK (playlist_type IN ('user', 'suggested', 'mood')),
    song_count INT,
    sync_state TEXT CHECK (sync_state IN ('pending', 'synced', 'error')) DEFAULT 'pending',
    last_synced_at TIMESTAMPTZ,
    candidate_playlist_ids JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE yt_tracks (
    id TEXT PRIMARY KEY,
    title TEXT,
    artist TEXT,
    album TEXT,
    duration_seconds INT,
    track_type TEXT CHECK (track_type IN ('user', 'suggested')),
    source_playlist_id TEXT REFERENCES yt_playlists(id) ON DELETE CASCADE,
    metadata_json JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Processing & Enrichment Tables
CREATE TABLE track_downloaded (
    video_id TEXT PRIMARY KEY REFERENCES yt_tracks(id) ON DELETE CASCADE,
    file_path TEXT,
    status TEXT CHECK (status IN ('pending', 'downloading', 'done', 'error')) DEFAULT 'pending',
    retries INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE local_clap (
    video_id TEXT PRIMARY KEY REFERENCES yt_tracks(id) ON DELETE CASCADE,
    file_path TEXT UNIQUE,
    embedding BYTEA,
    status TEXT CHECK (status IN ('pending', 'processing', 'done', 'error')) DEFAULT 'pending',
    retries INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE local_mert (
    video_id TEXT PRIMARY KEY REFERENCES yt_tracks(id) ON DELETE CASCADE,
    file_path TEXT UNIQUE,
    embedding BYTEA,
    status TEXT CHECK (status IN ('pending', 'processing', 'done', 'error')) DEFAULT 'pending',
    retries INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE local_librosa (
    video_id TEXT PRIMARY KEY REFERENCES yt_tracks(id) ON DELETE CASCADE,
    file_path TEXT UNIQUE,
    features BYTEA,
    status TEXT CHECK (status IN ('pending', 'processing', 'done', 'error')) DEFAULT 'pending',
    retries INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3. Indexes for fast queue lookups
CREATE INDEX idx_track_downloaded_status ON track_downloaded(status);
CREATE INDEX idx_local_clap_status ON local_clap(status);
CREATE INDEX idx_local_mert_status ON local_mert(status);
CREATE INDEX idx_local_librosa_status ON local_librosa(status);
