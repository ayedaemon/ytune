from __future__ import annotations

import json

import asyncpg

from core.db.local_mert import reset_many, status_counts


async def _list_errors(conn: asyncpg.Connection, limit: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT lm.video_id, yt.title, yt.artist, lm.retries, lm.error_message, lm.updated_at
        FROM local_mert lm
        JOIN yt_tracks yt ON yt.id = lm.video_id
        WHERE lm.status = 'error'
        ORDER BY lm.updated_at DESC
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


async def get_track_mert(conn: asyncpg.Connection, video_id: str) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT video_id, status, file_path, embedding, retries, error_message, updated_at
        FROM local_mert
        WHERE video_id = $1
        """,
        video_id,
    )
    if row is None:
        return None
    raw_embedding = row["embedding"]
    embedding = json.loads(raw_embedding) if isinstance(raw_embedding, (str, bytes)) else raw_embedding
    return {
        "video_id": row["video_id"],
        "status": row["status"],
        "file_path": row["file_path"],
        "embedding": embedding,
        "retries": row["retries"],
        "error_message": row["error_message"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def bulk_trigger_embed(
    conn: asyncpg.Connection, video_ids: list[str] | None, force: bool
) -> dict:
    return await reset_many(conn, video_ids, force)
