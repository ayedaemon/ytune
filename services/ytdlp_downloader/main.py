"""
ytdlp-downloader worker entrypoint.

Lifecycle:
  1. Connect pool.
  2. Recover any rows stuck in 'downloading' from a previous crash.
  3. Open a dedicated LISTEN connection on 'track_download_queue'.
  4. Drain any pending downloads queued before this process started.
  5. Block on the queue; drain the backlog per wakeup.
"""
import asyncio
import json

import asyncpg
import structlog

from core.config import DownloaderSettings
from core.logging import configure_logging
from services.ytdlp_downloader.app.domain.download import process_pending_downloads

settings = DownloaderSettings()
configure_logging("ytdlp-downloader", settings.log_level)
log = structlog.get_logger()

_STUCK_TIMEOUT = "10 minutes"


async def _recover_stuck(pool: asyncpg.Pool) -> None:
    """Reset any rows left in 'downloading' from a prior crash."""
    async with pool.acquire() as conn:
        affected = await conn.fetchval(
            f"""
            WITH updated AS (
                UPDATE track_downloaded
                SET status = 'pending', updated_at = NOW()
                WHERE status = 'downloading'
                  AND updated_at < NOW() - INTERVAL '{_STUCK_TIMEOUT}'
                RETURNING 1
            )
            SELECT COUNT(*) FROM updated
            """
        )
        if affected:
            log.info("recovered_stuck_rows", table="track_downloaded", count=int(affected))


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
    await listen_conn.add_listener("track_download_queue", on_notify)
    log.info("listening", channel="track_download_queue")

    # Drain any rows queued before this process started
    await wakeup.put({"event": "startup_drain"})

    while True:
        data = await wakeup.get()
        log.info("wakeup", trigger=data.get("event", "notify"))
        await process_pending_downloads(pool, settings)


if __name__ == "__main__":
    asyncio.run(main())
