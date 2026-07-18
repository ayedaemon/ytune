"""
ytm-sync worker entrypoint.

Lifecycle:
  1. Connect pool.
  2. Recover any rows stuck in 'processing' from a previous crash.
  3. Open a dedicated LISTEN connection on 'sync_jobs_queue'.
  4. Drain any sync_jobs already queued before this process started.
  5. Block on the queue; process one job per wakeup.
"""
import asyncio
import json

import asyncpg
import structlog

from core.config import YTMSyncSettings
from core.logging import configure_logging
from services.ytm_sync.app.domain.sync import run_sync

settings = YTMSyncSettings()
configure_logging("ytm-sync", settings.log_level)
log = structlog.get_logger()

_STUCK_TIMEOUT = "10 minutes"


async def _recover_stuck(pool: asyncpg.Pool) -> None:
    """Reset any rows left in 'processing' / 'downloading' from a prior crash."""
    async with pool.acquire() as conn:
        for table in ("track_downloaded", "local_librosa", "local_clap", "local_mert"):
            affected = await conn.fetchval(
                f"""
                WITH updated AS (
                    UPDATE {table}
                    SET status = 'pending', updated_at = NOW()
                    WHERE status IN ('processing', 'downloading')
                      AND updated_at < NOW() - INTERVAL '{_STUCK_TIMEOUT}'
                    RETURNING 1
                )
                SELECT COUNT(*) FROM updated
                """
            )
            if affected:
                log.info("recovered_stuck_rows", table=table, count=int(affected))


async def main() -> None:
    log.info("starting")

    pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=1,
        max_size=5,
    )

    await _recover_stuck(pool)

    wakeup: asyncio.Queue = asyncio.Queue()

    async def on_notify(
        conn: asyncpg.Connection, pid: int, channel: str, payload: str
    ) -> None:
        try:
            data = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            data = {}
        await wakeup.put(data)

    listen_conn = await asyncpg.connect(settings.database_url)
    await listen_conn.add_listener("sync_jobs_queue", on_notify)
    log.info("listening", channel="sync_jobs_queue")

    # Drain any jobs queued before this process started
    await wakeup.put({"event": "startup_drain"})

    while True:
        data = await wakeup.get()
        log.info("wakeup", trigger=data.get("event", "notify"))
        await run_sync(pool, settings.ytm_auth_file)


if __name__ == "__main__":
    asyncio.run(main())
