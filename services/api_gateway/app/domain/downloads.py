"""
Download queue control. Thin wrappers over core.db.track_downloaded, plus one
presentation-only query (needs a yt_tracks JOIN for title/artist) that's only
used here, so it isn't worth pushing into the shared module.
"""
from __future__ import annotations

import asyncpg

from core.db.track_downloaded import get, reset_many, status_counts


async def _list_errors(conn: asyncpg.Connection, limit: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT td.video_id, yt.title, yt.artist, td.retries, td.error_message, td.updated_at
        FROM track_downloaded td
        JOIN yt_tracks yt ON yt.id = td.video_id
        WHERE td.status = 'error'
        ORDER BY td.updated_at DESC
        LIMIT $1
        """,
        limit,
    )
    return [
        {
            "video_id": r["video_id"],
            "title": r["title"],
            "artist": r["artist"],
            "retries": r["retries"],
            "error_message": r["error_message"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


async def get_queue_status(conn: asyncpg.Connection, errors_limit: int = 20) -> dict:
    counts = await status_counts(conn)
    errors = await _list_errors(conn, errors_limit)
    return {
        "counts": {k: counts.get(k, 0) for k in ("pending", "downloading", "done", "error")},
        "errors": errors,
    }


async def get_track_download(conn: asyncpg.Connection, video_id: str) -> dict | None:
    row = await get(conn, video_id)
    if row is None:
        return None
    return {
        "video_id": row["video_id"],
        "status": row["status"],
        "file_path": row["file_path"],
        "retries": row["retries"],
        "error_message": row["error_message"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def bulk_trigger_download(
    conn: asyncpg.Connection, video_ids: list[str] | None, force: bool
) -> dict:
    """video_ids=None triggers every eligible track in the table."""
    return await reset_many(conn, video_ids, force)
