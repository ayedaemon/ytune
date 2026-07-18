from __future__ import annotations

import asyncpg

from core.db.local_mert import claim_pending as _claim_pending
from core.db.local_mert import mark_done as _mark_done
from core.db.local_mert import mark_failed_or_retry as _mark_failed_or_retry


class MertRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._c = conn

    async def claim_pending(self, limit: int = 5) -> list[asyncpg.Record]:
        return await _claim_pending(self._c, limit)

    async def mark_done(self, video_id: str, embedding: list[float]) -> None:
        await _mark_done(self._c, video_id, embedding)

    async def mark_failed_or_retry(self, video_id: str, error: str, max_retries: int = 3) -> None:
        await _mark_failed_or_retry(self._c, video_id, error, max_retries)
