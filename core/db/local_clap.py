"""
Shared SQL for the local_clap table. ytm-sync seeds pending rows,
ytdlp-downloader fills in file_path once a track is downloaded, embeddings-clap
claims/completes them, api-gateway reads status and can force a re-embed —
every read/write against this table goes through here so services never drift
on its shape.
"""
from __future__ import annotations

import json

import asyncpg

from core.db import bulk


async def get(conn: asyncpg.Connection, video_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT video_id, status, file_path, retries, error_message, updated_at
        FROM local_clap
        WHERE video_id = $1
        """,
        video_id,
    )


async def status_counts(conn: asyncpg.Connection) -> dict[str, int]:
    rows = await conn.fetch("SELECT status, COUNT(*) AS count FROM local_clap GROUP BY status")
    return {r["status"]: r["count"] for r in rows}


async def claim_pending(conn: asyncpg.Connection, limit: int = 10) -> list[asyncpg.Record]:
    """
    Atomically claim up to `limit` pending rows that actually have a file to embed —
    SELECT ... FOR UPDATE SKIP LOCKED and the status flip happen in the same transaction
    so two workers never grab the same row.
    """
    async with conn.transaction():
        rows = await conn.fetch(
            """
            SELECT video_id, file_path, retries FROM local_clap
            WHERE status = 'pending' AND file_path IS NOT NULL
            ORDER BY created_at
            LIMIT $1
            FOR UPDATE SKIP LOCKED
            """,
            limit,
        )
        if rows:
            await conn.execute(
                "UPDATE local_clap SET status='processing', updated_at=NOW() WHERE video_id = ANY($1)",
                [r["video_id"] for r in rows],
            )
    return list(rows)


async def mark_done(conn: asyncpg.Connection, video_id: str, embedding: list[float]) -> None:
    await conn.execute(
        """
        UPDATE local_clap SET status='done', embedding=$1, updated_at=NOW()
        WHERE video_id=$2
        """,
        json.dumps(embedding),
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
            UPDATE local_clap SET status='error', error_message=$1, updated_at=NOW()
            WHERE video_id=$2
            """,
            error,
            video_id,
        )
    else:
        await conn.execute(
            """
            UPDATE local_clap SET status='pending', retries=retries+1, error_message=$1, updated_at=NOW()
            WHERE video_id=$2
            """,
            error,
            video_id,
        )


async def reset_to_pending(conn: asyncpg.Connection, video_id: str) -> None:
    await conn.execute(
        """
        UPDATE local_clap SET status='pending', retries=0, error_message=NULL, updated_at=NOW()
        WHERE video_id=$1
        """,
        video_id,
    )


async def notify(conn: asyncpg.Connection, video_id: str, event: str = "track_added") -> None:
    payload = json.dumps({"video_id": video_id, "event": event})
    await conn.execute("SELECT pg_notify('track_clap_queue', $1)", payload)


async def reset_many(conn: asyncpg.Connection, video_ids: list[str] | None = None, force: bool = False) -> dict:
    """video_ids=None resets every eligible row in the table."""
    return await bulk.reset_many(
        conn, "local_clap", "processing", "track_clap_queue", video_ids, force,
        require_file_path=True,
    )
