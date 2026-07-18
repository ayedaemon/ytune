"""
DB access layer. All SQL lives here. Returns plain dicts — domain layer never sees asyncpg Records.
Adapts to the actual schema in 001_initial_schema.sql:
  - track_downloaded / local_* use video_id as primary key (not id SERIAL)
  - enrichment tables may lack error_message until migration 002 runs
"""
from __future__ import annotations

import json
from typing import Any

import asyncpg


class SyncJobRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def get_running(self) -> dict | None:
        row = await self._conn.fetchrow(
            "SELECT id, status FROM sync_jobs WHERE status IN ('queued','running') LIMIT 1"
        )
        return dict(row) if row else None

    async def create(self) -> str:
        job_id = await self._conn.fetchval(
            "INSERT INTO sync_jobs (status) VALUES ('queued') RETURNING id"
        )
        return str(job_id)

    async def get_latest(self) -> dict | None:
        row = await self._conn.fetchrow(
            """
            SELECT id, status, started_at, finished_at, stats, error
            FROM sync_jobs
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        return dict(row) if row else None

    async def get_by_id(self, job_id: str) -> dict | None:
        row = await self._conn.fetchrow(
            "SELECT id, status, started_at, finished_at, stats, error FROM sync_jobs WHERE id=$1",
            job_id,
        )
        return dict(row) if row else None


class PlaylistRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def list(
        self,
        playlist_type: str | None = None,
        limit: int = 50,
        after_id: str | None = None,
    ) -> list[dict]:
        conditions: list[str] = []
        args: list[Any] = []
        idx = 1

        if playlist_type:
            conditions.append(f"playlist_type = ${idx}")
            args.append(playlist_type)
            idx += 1

        if after_id:
            conditions.append(f"id > ${idx}")
            args.append(after_id)
            idx += 1

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        args.append(limit)

        rows = await self._conn.fetch(
            f"""
            SELECT id, title, owner, playlist_type, song_count, sync_state, last_synced_at,
                   created_at, updated_at
            FROM yt_playlists
            {where}
            ORDER BY id
            LIMIT ${idx}
            """,
            *args,
        )
        return [dict(r) for r in rows]

    async def get(self, playlist_id: str) -> dict | None:
        row = await self._conn.fetchrow(
            """
            SELECT id, title, owner, playlist_type, song_count, sync_state,
                   last_synced_at, candidate_playlist_ids, created_at, updated_at
            FROM yt_playlists
            WHERE id = $1
            """,
            playlist_id,
        )
        return dict(row) if row else None

    async def get_tracks(
        self, playlist_id: str, limit: int = 50, after_id: str | None = None
    ) -> list[dict]:
        conditions = ["t.source_playlist_id = $1"]
        args: list[Any] = [playlist_id]
        idx = 2

        if after_id:
            conditions.append(f"t.id > ${idx}")
            args.append(after_id)
            idx += 1

        args.append(limit)
        where = " AND ".join(conditions)

        rows = await self._conn.fetch(
            f"""
            SELECT t.id, t.title, t.artist, t.album, t.duration_seconds, t.track_type,
                   td.status  AS download_status,
                   ll.status  AS librosa_status,
                   lc.status  AS clap_status,
                   lm.status  AS mert_status
            FROM yt_tracks t
            LEFT JOIN track_downloaded td ON td.video_id = t.id
            LEFT JOIN local_librosa    ll ON ll.video_id = t.id
            LEFT JOIN local_clap       lc ON lc.video_id = t.id
            LEFT JOIN local_mert       lm ON lm.video_id = t.id
            WHERE {where}
            ORDER BY t.id
            LIMIT ${idx}
            """,
            *args,
        )
        return [dict(r) for r in rows]


def _escape_like(term: str) -> str:
    """Escape LIKE/ILIKE wildcards so a literal '%' or '_' in the search term is matched literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class TrackRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def list(
        self,
        track_type: str | None = None,
        has_download: bool | None = None,
        has_librosa: bool | None = None,
        has_clap: bool | None = None,
        has_mert: bool | None = None,
        has_errors: bool | None = None,
        q: str | None = None,
        limit: int = 50,
        after_id: str | None = None,
    ) -> list[dict]:
        conditions: list[str] = []
        args: list[Any] = []
        idx = 1

        if track_type:
            conditions.append(f"t.track_type = ${idx}")
            args.append(track_type)
            idx += 1

        if q:
            conditions.append(f"(t.title ILIKE ${idx} OR t.artist ILIKE ${idx})")
            args.append(f"%{_escape_like(q)}%")
            idx += 1

        if has_download is True:
            conditions.append("td.status = 'done'")
        elif has_download is False:
            conditions.append("(td.status IS NULL OR td.status != 'done')")

        for alias, has_flag in (("ll", has_librosa), ("lc", has_clap), ("lm", has_mert)):
            if has_flag is True:
                conditions.append(f"{alias}.status = 'done'")
            elif has_flag is False:
                conditions.append(f"({alias}.status IS NULL OR {alias}.status != 'done')")

        if has_errors is True:
            conditions.append(
                "(td.status = 'error' OR ll.status = 'error' OR lc.status = 'error' OR lm.status = 'error')"
            )

        if after_id:
            conditions.append(f"t.id > ${idx}")
            args.append(after_id)
            idx += 1

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        args.append(limit)

        rows = await self._conn.fetch(
            f"""
            SELECT t.id, t.title, t.artist, t.duration_seconds, t.track_type,
                   td.status AS download_status,
                   ll.status AS librosa_status,
                   lc.status AS clap_status,
                   lm.status AS mert_status
            FROM yt_tracks t
            LEFT JOIN track_downloaded td ON td.video_id = t.id
            LEFT JOIN local_librosa    ll ON ll.video_id = t.id
            LEFT JOIN local_clap       lc ON lc.video_id = t.id
            LEFT JOIN local_mert       lm ON lm.video_id = t.id
            {where}
            ORDER BY t.id
            LIMIT ${idx}
            """,
            *args,
        )
        return [dict(r) for r in rows]

    async def get(self, video_id: str, include_features: bool = False) -> dict | None:
        feature_cols = ""
        if include_features:
            feature_cols = ", ll.features, lc.embedding AS clap_embedding, lm.embedding AS mert_embedding"

        row = await self._conn.fetchrow(
            f"""
            SELECT t.id, t.title, t.artist, t.album, t.duration_seconds,
                   t.track_type, t.source_playlist_id, t.metadata_json,
                   t.created_at, t.updated_at,
                   td.status    AS download_status,
                   td.file_path AS download_file_path,
                   ll.status    AS librosa_status,
                   ll.features  AS librosa_features,
                   lc.status    AS clap_status,
                   lm.status    AS mert_status
                   {feature_cols}
            FROM yt_tracks t
            LEFT JOIN track_downloaded td ON td.video_id = t.id
            LEFT JOIN local_librosa    ll ON ll.video_id = t.id
            LEFT JOIN local_clap       lc ON lc.video_id = t.id
            LEFT JOIN local_mert       lm ON lm.video_id = t.id
            WHERE t.id = $1
            """,
            video_id,
        )
        return dict(row) if row else None
