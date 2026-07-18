# Service: ytdlp-downloader

## Purpose

Listens on `track_download_queue`. For each pending `track_downloaded` row:
1. Claims row with `FOR UPDATE SKIP LOCKED`
2. Downloads audio via yt-dlp to `STORAGE_ROOT/<video_id>.opus`
3. Updates `track_downloaded.status = 'done'`, `file_path = <real path>`
4. Updates `local_librosa`, `local_clap`, `local_mert` rows to replace placeholder path with real path
5. Checks if N tracks are fully enriched → emits `NOTIFY suggestion_engine_queue`

## Directory Layout

```
services/ytdlp_downloader/
├── Dockerfile
├── requirements.txt
├── main.py
└── app/
    ├── domain/
    │   └── download.py         # yt-dlp invocation, error classification
    └── infra/
        ├── db/
        │   └── repositories.py
        └── storage/
            └── fs.py           # file path resolution
```

## Key Files

### `main.py`

```python
import asyncio, asyncpg, json
from core.config import Settings
from core.logging import configure_logging
from app.domain.download import process_pending_downloads
import structlog

settings = Settings()
configure_logging("ytdlp-downloader", settings.log_level)
log = structlog.get_logger()

async def main():
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)

    # Recover stuck rows
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE track_downloaded SET status='pending', updated_at=NOW()
            WHERE status='downloading' AND updated_at < NOW() - INTERVAL '10 minutes'
        """)

    wakeup: asyncio.Queue = asyncio.Queue()

    async def on_notify(conn, pid, channel, payload):
        await wakeup.put({})

    listen_conn = await asyncpg.connect(settings.database_url)
    await listen_conn.add_listener("track_download_queue", on_notify)

    await wakeup.put({})  # drain pending on startup

    while True:
        await wakeup.get()
        async with pool.acquire() as conn:
            await process_pending_downloads(conn, settings)

asyncio.run(main())
```

### `app/domain/download.py`

```python
import asyncio, structlog
from app.infra.db.repositories import DownloadRepository
from app.infra.storage.fs import get_output_path
from core.utils.retry import download_retry

log = structlog.get_logger()
MAX_CONCURRENT = 3  # overridden by env via settings

async def process_pending_downloads(conn, settings):
    repo = DownloadRepository(conn)
    semaphore = asyncio.Semaphore(int(settings.max_concurrent_downloads))

    rows = await repo.claim_pending(limit=10)
    if not rows:
        return

    async def handle_one(row):
        async with semaphore:
            video_id = row["video_id"]
            structlog.contextvars.bind_contextvars(video_id=video_id)
            try:
                file_path = await download_track(video_id, settings.storage_root)
                await repo.mark_done(row["id"], file_path)
                await repo.update_enrichment_paths(video_id, file_path)
                log.info("download_done", file_path=file_path)
            except Exception as exc:
                log.error("download_failed", error=str(exc))
                await repo.mark_failed_or_retry(row["id"], str(exc))

    await asyncio.gather(*[handle_one(r) for r in rows])

@download_retry
async def download_track(video_id: str, storage_root: str) -> str:
    import yt_dlp
    output_path = get_output_path(storage_root, video_id)
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_path,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "opus",
        }],
        "quiet": True,
        "no_warnings": True,
    }
    loop = asyncio.get_event_loop()
    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    await loop.run_in_executor(None, _run)
    return output_path + ".opus"
```

### `app/infra/storage/fs.py`

```python
import os

def get_output_path(storage_root: str, video_id: str) -> str:
    os.makedirs(storage_root, exist_ok=True)
    return os.path.join(storage_root, video_id)
```

### `app/infra/db/repositories.py`

```python
import asyncpg

class DownloadRepository:
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def claim_pending(self, limit: int = 10) -> list:
        async with self.conn.transaction():
            rows = await self.conn.fetch("""
                SELECT id, video_id FROM track_downloaded
                WHERE status = 'pending'
                ORDER BY created_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
            """, limit)
            if rows:
                await self.conn.execute(
                    "UPDATE track_downloaded SET status='downloading', updated_at=NOW() WHERE id = ANY($1)",
                    [r["id"] for r in rows]
                )
            return list(rows)

    async def mark_done(self, row_id: int, file_path: str):
        await self.conn.execute("""
            UPDATE track_downloaded SET status='done', file_path=$1, updated_at=NOW() WHERE id=$2
        """, file_path, row_id)

    async def update_enrichment_paths(self, video_id: str, file_path: str):
        for table in ("local_librosa", "local_clap", "local_mert"):
            await self.conn.execute(f"""
                UPDATE {table} SET file_path=$1, updated_at=NOW()
                WHERE video_id=$2 AND (file_path IS NULL OR file_path LIKE 'yt://%')
            """, file_path, video_id)

    async def mark_failed_or_retry(self, row_id: int, error: str, max_retries: int = 3):
        row = await self.conn.fetchrow("SELECT retries FROM track_downloaded WHERE id=$1", row_id)
        if row["retries"] >= max_retries:
            await self.conn.execute(
                "UPDATE track_downloaded SET status='error', error_message=$1, updated_at=NOW() WHERE id=$2",
                error, row_id
            )
        else:
            await self.conn.execute(
                "UPDATE track_downloaded SET status='pending', retries=retries+1, updated_at=NOW() WHERE id=$1",
                row_id
            )
```

## `requirements.txt`

```
asyncpg==0.29.0
yt-dlp==2024.5.27
pydantic-settings==2.3.1
structlog==24.1.0
tenacity==8.3.0
```

## `Dockerfile`

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install uv --no-cache-dir
COPY core/ ./core/
COPY services/ytdlp_downloader/requirements.txt ./requirements.txt
RUN uv pip install --system -r requirements.txt
COPY services/ytdlp_downloader/ ./services/ytdlp_downloader/
ENV PYTHONPATH=/app
CMD ["python", "-m", "services.ytdlp_downloader.main"]
```

## Environment Variables

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `DATABASE_URL` | yes | — | |
| `STORAGE_ROOT` | yes | — | `/storage` in container |
| `MAX_CONCURRENT_DOWNLOADS` | no | `3` | |
| `LOG_LEVEL` | no | `INFO` | |

## Notes

- yt-dlp is CPU/IO bound. Run in executor to avoid blocking event loop.
- `ffmpeg` must be installed in the container (see Dockerfile).
- Downloaded files stored as `<STORAGE_ROOT>/<video_id>.opus`.
- After successful download, enrichment service rows get their `file_path` updated from
  placeholder `yt://<video_id>` to the real path. Librosa/CLAP/MERT workers will only
  pick up rows where `file_path` does NOT start with `yt://`.
