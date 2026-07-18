# Service: enrich-librosa

## Purpose

Listens on `track_librosa_queue`. For each pending `local_librosa` row with a real `file_path`
(not a `yt://` placeholder), runs librosa feature extraction and stores results as JSONB.

## Extracted Features

| Feature | Description |
|---------|-------------|
| `tempo` | BPM estimate |
| `duration` | Track duration in seconds |
| `key` | Estimated musical key (0–11) |
| `mode` | Major (1) or minor (0) |
| `spectral_centroid_mean` | Brightness proxy |
| `spectral_rolloff_mean` | |
| `zero_crossing_rate_mean` | Noisiness proxy |
| `mfcc_mean` | 13-dim MFCC mean vector |
| `rms_mean` | Energy/loudness proxy |

## Directory Layout

```
services/enrich_librosa/
├── Dockerfile
├── requirements.txt
├── main.py
└── app/
    ├── domain/
    │   └── analysis.py         # librosa feature extraction
    └── infra/
        └── db/
            └── repositories.py
```

## Key Files

### `main.py`

```python
import asyncio, asyncpg
from core.config import Settings
from core.logging import configure_logging
from app.domain.analysis import process_pending_librosa

settings = Settings()
configure_logging("enrich-librosa", settings.log_level)

async def main():
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE local_librosa SET status='pending', updated_at=NOW()
            WHERE status='processing' AND updated_at < NOW() - INTERVAL '10 minutes'
        """)

    wakeup: asyncio.Queue = asyncio.Queue()

    async def on_notify(conn, pid, channel, payload):
        await wakeup.put({})

    listen_conn = await asyncpg.connect(settings.database_url)
    await listen_conn.add_listener("track_librosa_queue", on_notify)
    await wakeup.put({})

    while True:
        await wakeup.get()
        async with pool.acquire() as conn:
            await process_pending_librosa(conn, settings)

asyncio.run(main())
```

### `app/domain/analysis.py`

```python
import asyncio, structlog
from concurrent.futures import ProcessPoolExecutor
from app.infra.db.repositories import LibrosaRepository

log = structlog.get_logger()
_pool = ProcessPoolExecutor(max_workers=2)

async def process_pending_librosa(conn, settings):
    repo = LibrosaRepository(conn)
    semaphore = asyncio.Semaphore(int(settings.max_concurrent_workers))
    rows = await repo.claim_pending(limit=5)

    async def handle_one(row):
        async with semaphore:
            structlog.contextvars.bind_contextvars(video_id=row["video_id"], file_path=row["file_path"])
            # Skip placeholder paths — file not downloaded yet
            if not row["file_path"] or row["file_path"].startswith("yt://"):
                await repo.release(row["id"])
                return
            try:
                features = await extract_features(row["file_path"])
                await repo.mark_done(row["id"], features)
                log.info("librosa_done")
            except Exception as exc:
                log.error("librosa_failed", error=str(exc))
                await repo.mark_failed_or_retry(row["id"], str(exc))

    await asyncio.gather(*[handle_one(r) for r in rows])

async def extract_features(file_path: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_pool, _sync_extract, file_path)

def _sync_extract(file_path: str) -> dict:
    import librosa, numpy as np
    y, sr = librosa.load(file_path, sr=None, mono=True, duration=300)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    key = int(np.argmax(np.mean(chroma, axis=1)))
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    return {
        "tempo":                    float(tempo),
        "duration":                 float(librosa.get_duration(y=y, sr=sr)),
        "key":                      key,
        "spectral_centroid_mean":   float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr))),
        "spectral_rolloff_mean":    float(np.mean(librosa.feature.spectral_rolloff(y=y, sr=sr))),
        "zero_crossing_rate_mean":  float(np.mean(librosa.feature.zero_crossing_rate(y))),
        "rms_mean":                 float(np.mean(librosa.feature.rms(y=y))),
        "mfcc_mean":                np.mean(mfcc, axis=1).tolist(),
    }
```

### `app/infra/db/repositories.py`

```python
import json
import asyncpg

class LibrosaRepository:
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def claim_pending(self, limit: int = 5) -> list:
        async with self.conn.transaction():
            rows = await self.conn.fetch("""
                SELECT id, video_id, file_path FROM local_librosa
                WHERE status = 'pending'
                  AND file_path IS NOT NULL
                  AND file_path NOT LIKE 'yt://%'
                ORDER BY created_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
            """, limit)
            if rows:
                await self.conn.execute(
                    "UPDATE local_librosa SET status='processing', updated_at=NOW() WHERE id = ANY($1)",
                    [r["id"] for r in rows]
                )
            return list(rows)

    async def release(self, row_id: int):
        # Put back to pending if file not ready yet
        await self.conn.execute(
            "UPDATE local_librosa SET status='pending', updated_at=NOW() WHERE id=$1", row_id
        )

    async def mark_done(self, row_id: int, features: dict):
        await self.conn.execute("""
            UPDATE local_librosa SET status='done', features=$1, updated_at=NOW() WHERE id=$2
        """, json.dumps(features), row_id)

    async def mark_failed_or_retry(self, row_id: int, error: str, max_retries: int = 3):
        row = await self.conn.fetchrow("SELECT retries FROM local_librosa WHERE id=$1", row_id)
        if row["retries"] >= max_retries:
            await self.conn.execute(
                "UPDATE local_librosa SET status='error', error_message=$1, updated_at=NOW() WHERE id=$2",
                error, row_id
            )
        else:
            await self.conn.execute(
                "UPDATE local_librosa SET status='pending', retries=retries+1, updated_at=NOW() WHERE id=$1",
                row_id
            )
```

## `requirements.txt`

```
asyncpg==0.29.0
librosa==0.10.2
soundfile==0.12.1
numpy==1.26.4
pydantic-settings==2.3.1
structlog==24.1.0
tenacity==8.3.0
```

## `Dockerfile`

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install uv --no-cache-dir
COPY core/ ./core/
COPY services/enrich_librosa/requirements.txt ./requirements.txt
RUN uv pip install --system -r requirements.txt
COPY services/enrich_librosa/ ./services/enrich_librosa/
ENV PYTHONPATH=/app
CMD ["python", "-m", "services.enrich_librosa.main"]
```

## Environment Variables

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `DATABASE_URL` | yes | — | |
| `STORAGE_ROOT` | yes | — | mounted read-only |
| `MAX_CONCURRENT_WORKERS` | no | `2` | |
| `LOG_LEVEL` | no | `INFO` | |

## Notes

- librosa is CPU-bound. Must run via `ProcessPoolExecutor`. Never call synchronously in async context.
- Clips at 300 seconds (`duration=300`) to avoid OOM on very long files.
- Rows with `file_path LIKE 'yt://%'` are skipped and released back to `pending` — file not
  downloaded yet. They will be re-notified when the downloader emits `track_librosa_queue` again,
  or picked up on next startup drain.
