# Service: embeddings-clap

## Purpose

Listens on `track_clap_queue`. For each pending `local_clap` row with a real `file_path`,
loads the LAION-CLAP model and computes a 512-dim audio embedding. Stores as JSONB.

## Model

**LAION-CLAP** (`laion-clap` package, `music_audioset_epoch_15_esc_90.14.pt` checkpoint or similar).
- Input: audio waveform (resampled to 48kHz)
- Output: 512-dim float vector
- Memory: ~1.5GB VRAM (GPU) or ~2GB RAM (CPU)

## Directory Layout

```
services/embeddings_clap/
├── Dockerfile
├── requirements.txt
├── main.py
└── app/
    ├── domain/
    │   └── embed.py            # CLAP model load + inference
    └── infra/
        └── db/
            └── repositories.py
```

## Key Files

### `main.py`

```python
import asyncio, asyncpg
from core.config import EmbeddingSettings
from core.logging import configure_logging
from app.domain.embed import ClapEmbedder, process_pending_clap

settings = EmbeddingSettings()
configure_logging("embeddings-clap", settings.log_level)

async def main():
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE local_clap SET status='pending', updated_at=NOW()
            WHERE status='processing' AND updated_at < NOW() - INTERVAL '10 minutes'
        """)

    # Load model once at startup (expensive)
    embedder = ClapEmbedder(model_path=settings.clap_model_path, device=settings.device)

    wakeup: asyncio.Queue = asyncio.Queue()

    async def on_notify(conn, pid, channel, payload):
        await wakeup.put({})

    listen_conn = await asyncpg.connect(settings.database_url)
    await listen_conn.add_listener("track_clap_queue", on_notify)
    await wakeup.put({})

    while True:
        await wakeup.get()
        async with pool.acquire() as conn:
            await process_pending_clap(conn, settings, embedder)

asyncio.run(main())
```

### `app/domain/embed.py`

```python
import asyncio, json, structlog
from concurrent.futures import ThreadPoolExecutor
from app.infra.db.repositories import ClapRepository

log = structlog.get_logger()
_executor = ThreadPoolExecutor(max_workers=1)  # model inference is not thread-safe for >1

class ClapEmbedder:
    def __init__(self, model_path: str, device: str = "cpu"):
        import laion_clap
        self.model = laion_clap.CLAP_Module(enable_fusion=False, device=device)
        self.model.load_ckpt(model_path)
        self.device = device

    def embed_file(self, file_path: str) -> list[float]:
        import numpy as np
        embedding = self.model.get_audio_embedding_from_filelist([file_path], use_tensor=False)
        return embedding[0].tolist()

async def process_pending_clap(conn, settings, embedder: ClapEmbedder):
    repo = ClapRepository(conn)
    semaphore = asyncio.Semaphore(int(settings.max_concurrent_workers))
    rows = await repo.claim_pending(limit=5)

    async def handle_one(row):
        async with semaphore:
            structlog.contextvars.bind_contextvars(video_id=row["video_id"])
            if not row["file_path"] or row["file_path"].startswith("yt://"):
                await repo.release(row["id"])
                return
            try:
                loop = asyncio.get_event_loop()
                embedding = await loop.run_in_executor(_executor, embedder.embed_file, row["file_path"])
                await repo.mark_done(row["id"], embedding)
                log.info("clap_done")
            except Exception as exc:
                log.error("clap_failed", error=str(exc))
                await repo.mark_failed_or_retry(row["id"], str(exc))

    await asyncio.gather(*[handle_one(r) for r in rows])
```

### `app/infra/db/repositories.py`

```python
import json, asyncpg

class ClapRepository:
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def claim_pending(self, limit: int = 5) -> list:
        async with self.conn.transaction():
            rows = await self.conn.fetch("""
                SELECT id, video_id, file_path FROM local_clap
                WHERE status = 'pending'
                  AND file_path IS NOT NULL
                  AND file_path NOT LIKE 'yt://%'
                ORDER BY created_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
            """, limit)
            if rows:
                await self.conn.execute(
                    "UPDATE local_clap SET status='processing', updated_at=NOW() WHERE id = ANY($1)",
                    [r["id"] for r in rows]
                )
            return list(rows)

    async def release(self, row_id: int):
        await self.conn.execute(
            "UPDATE local_clap SET status='pending', updated_at=NOW() WHERE id=$1", row_id
        )

    async def mark_done(self, row_id: int, embedding: list[float]):
        await self.conn.execute("""
            UPDATE local_clap SET status='done', embedding=$1, updated_at=NOW() WHERE id=$2
        """, json.dumps(embedding), row_id)

    async def mark_failed_or_retry(self, row_id: int, error: str, max_retries: int = 3):
        row = await self.conn.fetchrow("SELECT retries FROM local_clap WHERE id=$1", row_id)
        if row["retries"] >= max_retries:
            await self.conn.execute(
                "UPDATE local_clap SET status='error', error_message=$1, updated_at=NOW() WHERE id=$2",
                error, row_id
            )
        else:
            await self.conn.execute(
                "UPDATE local_clap SET status='pending', retries=retries+1, updated_at=NOW() WHERE id=$1",
                row_id
            )
```

## `requirements.txt`

```
asyncpg==0.29.0
laion-clap==1.1.4
torch==2.3.0
torchaudio==2.3.0
soundfile==0.12.1
pydantic-settings==2.3.1
structlog==24.1.0
tenacity==8.3.0
```

## `Dockerfile`

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 ffmpeg git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install uv --no-cache-dir
COPY core/ ./core/
COPY services/embeddings_clap/requirements.txt ./requirements.txt
RUN uv pip install --system -r requirements.txt
COPY services/embeddings_clap/ ./services/embeddings_clap/
ENV PYTHONPATH=/app
CMD ["python", "-m", "services.embeddings_clap.main"]
```

## Environment Variables

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `DATABASE_URL` | yes | — | |
| `STORAGE_ROOT` | yes | — | mounted read-only |
| `CLAP_MODEL_PATH` | yes | `/models/clap` | path to downloaded checkpoint |
| `DEVICE` | no | `cpu` | `cpu` or `cuda` |
| `MAX_CONCURRENT_WORKERS` | no | `1` | keep at 1 unless GPU |
| `LOG_LEVEL` | no | `INFO` | |

## Notes

- Model is loaded **once at startup**, not per-request. Cold start ~10–30s.
- `ThreadPoolExecutor(max_workers=1)` because CLAP model is not thread-safe for concurrent
  inference. Increase only if you have multiple GPUs.
- Pre-download the model checkpoint before starting the service:
  ```bash
  make download-models
  # or manually:
  docker compose run --rm embeddings-clap python -c "
  import laion_clap; m = laion_clap.CLAP_Module(); m.load_ckpt()"
  ```
- Embedding is stored as a JSONB array of 512 floats. Migrate to `pgvector vector(512)` for
  efficient cosine similarity queries.
