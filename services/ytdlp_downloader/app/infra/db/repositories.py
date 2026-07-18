"""
DB access layer for ytdlp-downloader.
Matches actual schema from 001_initial_schema.sql / 002_sync_jobs_and_error_columns.sql:
  - track_downloaded: video_id TEXT PRIMARY KEY (no separate id column)
  - local_librosa / local_clap / local_mert: video_id TEXT PRIMARY KEY, file_path
    starts NULL and is filled in here once the real file exists.
"""
from __future__ import annotations

import json

import asyncpg

from core.db.track_downloaded import mark_done as _mark_done
from core.db.track_downloaded import mark_failed_or_retry as _mark_failed_or_retry


class DownloadRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._c = conn

    async def claim_pending(self, limit: int = 10) -> list[asyncpg.Record]:
        """
        Atomically claim up to `limit` pending rows — SELECT ... FOR UPDATE SKIP LOCKED
        and the status flip happen in the same transaction so two workers never grab
        the same row.
        """
        async with self._c.transaction():
            rows = await self._c.fetch(
                """
                SELECT video_id, retries FROM track_downloaded
                WHERE status = 'pending'
                ORDER BY created_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
                """,
                limit,
            )
            if rows:
                await self._c.execute(
                    "UPDATE track_downloaded SET status='downloading', updated_at=NOW() WHERE video_id = ANY($1)",
                    [r["video_id"] for r in rows],
                )
        return list(rows)

    async def mark_done(self, video_id: str, file_path: str) -> None:
        await _mark_done(self._c, video_id, file_path)

    async def update_enrichment_paths(self, video_id: str, file_path: str) -> None:
        """Copy the real path into each enrichment table — that's the workers' pending-work signal."""
        for table in ("local_librosa", "local_clap", "local_mert"):
            await self._c.execute(
                f"""
                UPDATE {table} SET file_path=$1, updated_at=NOW()
                WHERE video_id=$2 AND file_path IS NULL
                """,
                file_path,
                video_id,
            )

    async def notify_enrichment_queues(self, video_id: str) -> None:
        payload = json.dumps({"video_id": video_id, "event": "download_done"})
        for channel in ("track_librosa_queue", "track_clap_queue", "track_mert_queue"):
            await self._c.execute("SELECT pg_notify($1, $2)", channel, payload)

    async def mark_failed_or_retry(self, video_id: str, error: str, max_retries: int = 3) -> None:
        await _mark_failed_or_retry(self._c, video_id, error, max_retries)
