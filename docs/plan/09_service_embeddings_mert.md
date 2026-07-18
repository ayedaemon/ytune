# Service: embeddings-mert

## Purpose

Listens on `track_mert_queue`. For each pending `local_mert` row with a real `file_path`,
loads the MERT model and computes a 768-dim audio representation. Stores as JSONB.

Structurally identical to `embeddings-clap`. Key differences:
- Different model (MERT-v1-95M or MERT-v1-330M from HuggingFace)
- Output dim: 768 (vs 512 for CLAP)
- Uses `transformers` library instead of `laion-clap`
- Listens on `track_mert_queue` (vs `track_clap_queue`)
- Writes to `local_mert` (vs `local_clap`)

## Model

**MERT** (`m-a-p/MERT-v1-95M` on HuggingFace).
- Input: audio waveform at 24kHz
- Output: mean-pooled hidden state from last transformer layer → 768-dim vector
- Memory: ~400MB (95M params) or ~1.2GB (330M params)

## Directory Layout

```
services/embeddings_mert/
├── Dockerfile
├── requirements.txt
├── main.py
└── app/
    ├── domain/
    │   └── embed.py
    └── infra/
        └── db/
            └── repositories.py
```

## Key Files

### `app/domain/embed.py`

```python
import asyncio, json, structlog
from concurrent.futures import ThreadPoolExecutor
from app.infra.db.repositories import MertRepository

log = structlog.get_logger()
_executor = ThreadPoolExecutor(max_workers=1)

class MertEmbedder:
    def __init__(self, model_path: str, device: str = "cpu"):
        from transformers import AutoModel, Wav2Vec2FeatureExtractor
        self.processor = Wav2Vec2FeatureExtractor.from_pretrained(model_path, trust_remote_code=True)
        self.model = AutoModel.from_pretrained(model_path, trust_remote_code=True)
        self.model.eval()
        self.device = device
        if device == "cuda":
            self.model = self.model.cuda()

    def embed_file(self, file_path: str) -> list[float]:
        import torch, librosa, numpy as np
        y, sr = librosa.load(file_path, sr=24000, mono=True, duration=300)
        inputs = self.processor(y, sampling_rate=24000, return_tensors="pt", padding=True)
        if self.device == "cuda":
            inputs = {k: v.cuda() for k, v in inputs.items()}
        with torch.no_grad():
            outputs = self.model(**inputs, output_hidden_states=True)
        # Mean-pool all hidden states from last layer
        hidden = outputs.hidden_states[-1]  # (1, T, 768)
        embedding = hidden.mean(dim=1).squeeze(0).cpu().numpy()
        return embedding.tolist()

async def process_pending_mert(conn, settings, embedder: MertEmbedder):
    repo = MertRepository(conn)
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
                log.info("mert_done")
            except Exception as exc:
                log.error("mert_failed", error=str(exc))
                await repo.mark_failed_or_retry(row["id"], str(exc))

    await asyncio.gather(*[handle_one(r) for r in rows])
```

### `app/infra/db/repositories.py`

```python
import json, asyncpg

class MertRepository:
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def claim_pending(self, limit: int = 5) -> list:
        async with self.conn.transaction():
            rows = await self.conn.fetch("""
                SELECT id, video_id, file_path FROM local_mert
                WHERE status = 'pending'
                  AND file_path IS NOT NULL
                  AND file_path NOT LIKE 'yt://%'
                ORDER BY created_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
            """, limit)
            if rows:
                await self.conn.execute(
                    "UPDATE local_mert SET status='processing', updated_at=NOW() WHERE id = ANY($1)",
                    [r["id"] for r in rows]
                )
            return list(rows)

    async def release(self, row_id: int):
        await self.conn.execute(
            "UPDATE local_mert SET status='pending', updated_at=NOW() WHERE id=$1", row_id
        )

    async def mark_done(self, row_id: int, embedding: list[float]):
        await self.conn.execute("""
            UPDATE local_mert SET status='done', embedding=$1, updated_at=NOW() WHERE id=$2
        """, json.dumps(embedding), row_id)

    async def mark_failed_or_retry(self, row_id: int, error: str, max_retries: int = 3):
        row = await self.conn.fetchrow("SELECT retries FROM local_mert WHERE id=$1", row_id)
        if row["retries"] >= max_retries:
            await self.conn.execute(
                "UPDATE local_mert SET status='error', error_message=$1, updated_at=NOW() WHERE id=$2",
                error, row_id
            )
        else:
            await self.conn.execute(
                "UPDATE local_mert SET status='pending', retries=retries+1, updated_at=NOW() WHERE id=$1",
                row_id
            )
```

## `requirements.txt`

```
asyncpg==0.29.0
torch==2.3.0
torchaudio==2.3.0
transformers==4.41.2
librosa==0.10.2
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
COPY services/embeddings_mert/requirements.txt ./requirements.txt
RUN uv pip install --system -r requirements.txt
COPY services/embeddings_mert/ ./services/embeddings_mert/
ENV PYTHONPATH=/app
CMD ["python", "-m", "services.embeddings_mert.main"]
```

## Environment Variables

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `DATABASE_URL` | yes | — | |
| `STORAGE_ROOT` | yes | — | mounted read-only |
| `MERT_MODEL_PATH` | yes | `/models/mert` | HuggingFace model dir |
| `DEVICE` | no | `cpu` | `cpu` or `cuda` |
| `MAX_CONCURRENT_WORKERS` | no | `1` | |
| `LOG_LEVEL` | no | `INFO` | |

## Pre-downloading the Model

```bash
# Run once before docker compose up
docker compose run --rm embeddings-mert python -c "
from transformers import AutoModel, Wav2Vec2FeatureExtractor
AutoModel.from_pretrained('m-a-p/MERT-v1-95M', trust_remote_code=True, cache_dir='/models/mert')
Wav2Vec2FeatureExtractor.from_pretrained('m-a-p/MERT-v1-95M', trust_remote_code=True, cache_dir='/models/mert')
print('MERT downloaded')
"
```

## Notes

- MERT processes audio at 24kHz (not 16kHz or 44.1kHz). librosa resamples automatically.
- `trust_remote_code=True` required for MERT custom modeling code from HuggingFace.
- OOM on CPU with 330M model + long tracks. Use 95M or limit `duration=300`.
- Embedding stored as 768-float JSONB array (~3KB/row). Migrate to `pgvector vector(768)` for
  similarity search.
