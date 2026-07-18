"""
Librosa analysis queue control. Thin wrappers over core.db.local_librosa, plus
one presentation-only query (needs a yt_tracks JOIN for title/artist) that's
only used here, so it isn't worth pushing into the shared module.
"""
from __future__ import annotations

import json

import asyncpg

from core.db.local_librosa import reset_many, status_counts


async def _list_errors(conn: asyncpg.Connection, limit: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT ll.video_id, yt.title, yt.artist, ll.retries, ll.error_message, ll.updated_at
        FROM local_librosa ll
        JOIN yt_tracks yt ON yt.id = ll.video_id
        WHERE ll.status = 'error'
        ORDER BY ll.updated_at DESC
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
        "counts": {k: counts.get(k, 0) for k in ("pending", "processing", "done", "error")},
        "errors": errors,
    }


async def get_track_librosa(conn: asyncpg.Connection, video_id: str) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT video_id, status, file_path, features, retries, error_message, updated_at
        FROM local_librosa
        WHERE video_id = $1
        """,
        video_id,
    )
    if row is None:
        return None
    raw_features = row["features"]
    features = json.loads(raw_features) if isinstance(raw_features, (str, bytes)) else raw_features
    return {
        "video_id": row["video_id"],
        "status": row["status"],
        "file_path": row["file_path"],
        "features": features,
        "retries": row["retries"],
        "error_message": row["error_message"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def bulk_trigger_analysis(
    conn: asyncpg.Connection, video_ids: list[str] | None, force: bool
) -> dict:
    """video_ids=None triggers every eligible track in the table."""
    return await reset_many(conn, video_ids, force)
