"""
Shared SQL for the track_downloaded table. ytm-sync creates rows, ytdlp-downloader
claims/completes them, api-gateway reads status and can force a re-download —
every read/write against this table goes through here so the three services
never drift on its shape.
"""
from __future__ import annotations

import json

import asyncpg

from core.db import bulk


async def ensure_row(conn: asyncpg.Connection, video_id: str) -> None:
    await conn.execute(
        """
        INSERT INTO track_downloaded (video_id, status)
        VALUES ($1, 'pending')
        ON CONFLICT (video_id) DO NOTHING
        """,
        video_id,
    )


async def get(conn: asyncpg.Connection, video_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT video_id, status, file_path, retries, error_message, updated_at
        FROM track_downloaded
        WHERE video_id = $1
        """,
        video_id,
    )


async def status_counts(conn: asyncpg.Connection) -> dict[str, int]:
    rows = await conn.fetch("SELECT status, COUNT(*) AS count FROM track_downloaded GROUP BY status")
    return {r["status"]: r["count"] for r in rows}


async def mark_done(conn: asyncpg.Connection, video_id: str, file_path: str) -> None:
    await conn.execute(
        """
        UPDATE track_downloaded SET status='done', file_path=$1, updated_at=NOW()
        WHERE video_id=$2
        """,
        file_path,
        video_id,
    )


async def mark_failed_or_retry(
    conn: asyncpg.Connection, video_id: str, error: str, max_retries: int = 3
) -> None:
    row = await get(conn, video_id)
    if row is None:
        return
    if row["retries"] >= max_retries:
        await conn.execute(
            """
            UPDATE track_downloaded SET status='error', error_message=$1, updated_at=NOW()
            WHERE video_id=$2
            """,
            error,
            video_id,
        )
    else:
        await conn.execute(
            """
            UPDATE track_downloaded SET status='pending', retries=retries+1, error_message=$1, updated_at=NOW()
            WHERE video_id=$2
            """,
            error,
            video_id,
        )


async def reset_to_pending(conn: asyncpg.Connection, video_id: str) -> None:
    await conn.execute(
        """
        UPDATE track_downloaded SET status='pending', retries=0, error_message=NULL, updated_at=NOW()
        WHERE video_id=$1
        """,
        video_id,
    )


async def notify(conn: asyncpg.Connection, video_id: str, event: str = "track_added") -> None:
    payload = json.dumps({"video_id": video_id, "event": event})
    await conn.execute("SELECT pg_notify('track_download_queue', $1)", payload)


async def reset_many(conn: asyncpg.Connection, video_ids: list[str] | None = None, force: bool = False) -> dict:
    """video_ids=None resets every eligible row in the table."""
    return await bulk.reset_many(conn, "track_downloaded", "downloading", "track_download_queue", video_ids, force)
