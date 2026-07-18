# Service: api-gateway

## Purpose

User-facing FastAPI HTTP API. Reads DB state, triggers syncs via `sync_jobs` + `pg_notify`,
returns enriched track/playlist/mood data. No direct calls to other internal services.

## Directory Layout

```
services/api_gateway/
├── Dockerfile
├── requirements.txt
├── main.py                     # uvicorn entrypoint
└── app/
    ├── api/
    │   ├── deps.py             # Depends(get_db_pool), Depends(get_settings)
    │   └── routes/
    │       ├── account.py      # POST /v1/account/sync, GET /v1/account/sync/status
    │       ├── playlists.py    # GET /v1/playlists, GET /v1/playlists/{id}
    │       ├── tracks.py       # GET /v1/tracks, GET /v1/tracks/{id}
    │       ├── mood.py         # GET /v1/mood/playlists, GET /v1/mood/playlists/{id}/tracks
    │       └── suggestion.py   # POST /v1/suggestion/similar, POST /v1/suggestion/recompute
    ├── domain/
    │   ├── sync.py             # create_sync_job(), get_sync_status()
    │   └── suggestions.py      # find_similar(), get_mood_playlists()
    └── infra/
        └── db/
            └── repositories.py # PlaylistRepo, TrackRepo, SyncJobRepo, MoodRepo
```

## Key Files

### `main.py`

```python
from fastapi import FastAPI
from contextlib import asynccontextmanager
import asyncpg
from core.config import Settings
from core.logging import configure_logging
from app.api.routes import account, playlists, tracks, mood, suggestion

settings = Settings()
configure_logging("api-gateway", settings.log_level)

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(settings.database_url,
                                                min_size=2,
                                                max_size=settings.db_max_pool_size)
    yield
    await app.state.pool.close()

app = FastAPI(title="YTune API", version="1.0.0", lifespan=lifespan)

app.include_router(account.router,    prefix="/v1/account",    tags=["account"])
app.include_router(playlists.router,  prefix="/v1/playlists",  tags=["playlists"])
app.include_router(tracks.router,     prefix="/v1/tracks",     tags=["tracks"])
app.include_router(mood.router,       prefix="/v1/mood",       tags=["mood"])
app.include_router(suggestion.router, prefix="/v1/suggestion", tags=["suggestion"])

@app.get("/health")
async def health():
    return {"status": "ok", "service": "api-gateway"}
```

### `app/api/deps.py`

```python
from fastapi import Request
import asyncpg

async def get_db_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool
```

### `app/api/routes/account.py`

```python
from fastapi import APIRouter, Depends, HTTPException
import asyncpg
from app.api.deps import get_db_pool
from app.domain.sync import create_sync_job, get_latest_sync_status

router = APIRouter()

@router.post("/sync", status_code=202)
async def trigger_sync(pool: asyncpg.Pool = Depends(get_db_pool)):
    async with pool.acquire() as conn:
        # Check for already-running sync
        running = await conn.fetchval(
            "SELECT id FROM sync_jobs WHERE status = 'running' LIMIT 1"
        )
        if running:
            raise HTTPException(409, detail={"error": {"code": "SYNC_ALREADY_RUNNING"}})
        sync_id = await create_sync_job(conn)
    return {"sync_id": str(sync_id), "status": "queued"}

@router.get("/sync/status")
async def sync_status(pool: asyncpg.Pool = Depends(get_db_pool)):
    async with pool.acquire() as conn:
        return await get_latest_sync_status(conn)
```

### `app/domain/sync.py`

```python
import uuid, json
import asyncpg

async def create_sync_job(conn: asyncpg.Connection) -> uuid.UUID:
    job_id = await conn.fetchval(
        "INSERT INTO sync_jobs (status) VALUES ('queued') RETURNING id"
    )
    payload = json.dumps({"sync_id": str(job_id)})
    await conn.execute("SELECT pg_notify('sync_jobs_queue', $1)", payload)
    return job_id

async def get_latest_sync_status(conn: asyncpg.Connection) -> dict:
    row = await conn.fetchrow(
        "SELECT id, status, started_at, finished_at, stats FROM sync_jobs ORDER BY created_at DESC LIMIT 1"
    )
    if not row:
        return {"status": "never_run"}
    return dict(row)
```

### `app/infra/db/repositories.py`

```python
from core.models.playlist import PlaylistRecord
from core.models.track import TrackRecord
import asyncpg

class PlaylistRepository:
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def list_playlists(self, playlist_type: str | None, limit: int, cursor: str | None) -> list[PlaylistRecord]:
        where = "WHERE playlist_type = $1" if playlist_type else ""
        args = [playlist_type] if playlist_type else []
        rows = await self.conn.fetch(f"SELECT * FROM yt_playlists {where} ORDER BY created_at LIMIT {limit}", *args)
        return [PlaylistRecord(**r) for r in rows]

    async def get_playlist(self, playlist_id: str) -> PlaylistRecord | None:
        row = await self.conn.fetchrow("SELECT * FROM yt_playlists WHERE id = $1", playlist_id)
        return PlaylistRecord(**row) if row else None

class TrackRepository:
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def get_track_with_enrichment(self, video_id: str) -> dict | None:
        row = await self.conn.fetchrow("""
            SELECT t.*, td.status AS download_status, td.file_path,
                   ll.status AS librosa_status, ll.features,
                   lc.status AS clap_status,
                   lm.status AS mert_status
            FROM yt_tracks t
            LEFT JOIN track_downloaded td ON td.video_id = t.id
            LEFT JOIN local_librosa    ll ON ll.video_id = t.id
            LEFT JOIN local_clap       lc ON lc.video_id = t.id
            LEFT JOIN local_mert       lm ON lm.video_id = t.id
            WHERE t.id = $1
        """, video_id)
        return dict(row) if row else None
```

## `requirements.txt`

```
fastapi==0.111.0
uvicorn[standard]==0.30.1
asyncpg==0.29.0
pydantic==2.7.1
pydantic-settings==2.3.1
structlog==24.1.0
```

## `Dockerfile`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install uv --no-cache-dir
COPY core/ ./core/
COPY services/api_gateway/requirements.txt ./requirements.txt
RUN uv pip install --system -r requirements.txt
COPY services/api_gateway/ ./services/api_gateway/
ENV PYTHONPATH=/app
CMD ["uvicorn", "services.api_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## Environment Variables

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `DATABASE_URL` | yes | — | `postgresql://user:pass@db:5432/ytune` |
| `LOG_LEVEL` | no | `INFO` | |
| `DB_MAX_POOL_SIZE` | no | `10` | |

## Layer Rules

- Route handlers: parse request, call domain, serialize response. No SQL.
- Domain functions: orchestrate, apply business rules. Accept/return Pydantic models.
- Repositories: SQL only. Return Pydantic models, never raw asyncpg `Record` objects.
