"""
DB access layer for ytm-sync.
Matches actual schema from 001_initial_schema.sql:
  - local_* tables: video_id TEXT PRIMARY KEY, file_path TEXT UNIQUE (nullable)
  - track_downloaded: video_id TEXT PRIMARY KEY
  - yt_playlists / yt_tracks: TEXT PRIMARY KEY
"""
from __future__ import annotations

import json

import asyncpg

from core.db.track_downloaded import ensure_row as ensure_download_row


class SyncJobRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._c = conn

    async def claim_queued(self) -> str | None:
        """
        Atomically transition one queued job to running.
        Returns its id, or None if nothing is queued.
        """
        job_id = await self._c.fetchval(
            """
            UPDATE sync_jobs SET status = 'running', started_at = NOW()
            WHERE id = (
                SELECT id FROM sync_jobs
                WHERE status = 'queued'
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id
            """
        )
        return str(job_id) if job_id else None

    async def finish(self, job_id: str, stats: dict) -> None:
        await self._c.execute(
            """
            UPDATE sync_jobs
            SET status = 'done', finished_at = NOW(), stats = $1
            WHERE id = $2
            """,
            json.dumps(stats),
            job_id,
        )

    async def fail(self, job_id: str, error: str, stats: dict) -> None:
        await self._c.execute(
            """
            UPDATE sync_jobs
            SET status = 'error', finished_at = NOW(), error = $1, stats = $2
            WHERE id = $3
            """,
            error,
            json.dumps(stats),
            job_id,
        )


class PlaylistRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._c = conn

    async def upsert(self, playlist: dict, playlist_type: str) -> None:
        await self._c.execute(
            """
            INSERT INTO yt_playlists (id, title, owner, playlist_type, song_count, sync_state, last_synced_at)
            VALUES ($1, $2, $3, $4, $5, 'synced', NOW())
            ON CONFLICT (id) DO UPDATE SET
                title          = EXCLUDED.title,
                owner          = EXCLUDED.owner,
                song_count     = EXCLUDED.song_count,
                sync_state     = 'synced',
                last_synced_at = NOW(),
                updated_at     = NOW()
            """,
            playlist["id"],
            playlist["title"],
            playlist.get("owner") or "",
            playlist_type,
            playlist.get("song_count") or 0,
        )

    async def upsert_many(self, playlists: list[dict], playlist_type: str) -> None:
        for pl in playlists:
            if pl.get("id"):
                await self.upsert(pl, playlist_type)

    async def update_song_count(self, playlist_id: str, count: int) -> None:
        await self._c.execute(
            "UPDATE yt_playlists SET song_count=$1, updated_at=NOW() WHERE id=$2",
            count,
            playlist_id,
        )


class TrackRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._c = conn

    async def upsert(self, track: dict, track_type: str, playlist_id: str) -> None:
        await self._c.execute(
            """
            INSERT INTO yt_tracks
                (id, title, artist, album, duration_seconds, track_type, source_playlist_id, metadata_json)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (id) DO UPDATE SET
                title            = EXCLUDED.title,
                artist           = EXCLUDED.artist,
                album            = EXCLUDED.album,
                duration_seconds = EXCLUDED.duration_seconds,
                metadata_json    = EXCLUDED.metadata_json,
                updated_at       = NOW()
            """,
            track["video_id"],
            track.get("title"),
            track.get("artist"),
            track.get("album"),
            track.get("duration_seconds"),
            track_type,
            playlist_id,
            json.dumps(track.get("raw") or {}),
        )

    async def upsert_many(
        self, tracks: list[dict], track_type: str, playlist_id: str
    ) -> int:
        count = 0
        for t in tracks:
            if t.get("video_id"):
                await self.upsert(t, track_type, playlist_id)
                count += 1
        return count

    async def ensure_processing_rows(self, video_id: str) -> None:
        """
        Create pending rows for each worker — idempotent.
        local_* tables: video_id is PK; file_path starts NULL and is filled by downloader.
        """
        await ensure_download_row(self._c, video_id)
        for table in ("local_librosa", "local_clap", "local_mert"):
            await self._c.execute(
                f"""
                INSERT INTO {table} (video_id, status)
                VALUES ($1, 'pending')
                ON CONFLICT (video_id) DO NOTHING
                """,
                video_id,
            )
