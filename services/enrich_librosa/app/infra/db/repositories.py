"""
DB access layer for enrich-librosa. All local_librosa SQL lives in
core.db.local_librosa — this class exists so app/domain/analysis.py has one
object to call, matching ytdlp_downloader's DownloadRepository shape.
"""
from __future__ import annotations

import asyncpg

from core.db.local_librosa import claim_pending as _claim_pending
from core.db.local_librosa import mark_done as _mark_done
from core.db.local_librosa import mark_failed_or_retry as _mark_failed_or_retry


class LibrosaRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._c = conn

    async def claim_pending(self, limit: int = 5) -> list[asyncpg.Record]:
        return await _claim_pending(self._c, limit)

    async def mark_done(self, video_id: str, features: dict) -> None:
        await _mark_done(self._c, video_id, features)

    async def mark_failed_or_retry(self, video_id: str, error: str, max_retries: int = 3) -> None:
        await _mark_failed_or_retry(self._c, video_id, error, max_retries)
