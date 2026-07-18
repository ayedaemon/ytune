# Service: ytm-sync

## Purpose

Async worker that:
1. Listens on `sync_jobs_queue` pg_notify channel
2. Calls ytmusicapi to fetch user playlists and tracks
3. Upserts `yt_playlists` and `yt_tracks`
4. Creates pending rows in `track_downloaded`, `local_librosa`, `local_clap`, `local_mert`
5. Emits pg_notify on 4 downstream channels per track

## Directory Layout

```
services/ytm_sync/
├── Dockerfile
├── requirements.txt
├── main.py                         # asyncio.run(main())
└── app/
    ├── domain/
    │   ├── sync.py                 # full sync orchestration
    │   └── playlists.py            # playlist processing rules
    └── infra/
        ├── ytm/
        │   └── client.py           # YTMClient wrapping ytmusicapi
        └── db/
            └── repositories.py
```

## Key Files

### `main.py`

```python
import asyncio, asyncpg, json
from core.config import Settings
from core.logging import configure_logging
from app.domain.sync import run_sync
import structlog

settings = Settings()
configure_logging("ytm-sync", settings.log_level)
log = structlog.get_logger()

async def main():
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)

    # Recover stuck processing rows from previous crash
    async with pool.acquire() as conn:
        for table in ("track_downloaded", "local_librosa", "local_clap", "local_mert"):
            await conn.execute(f"""
                UPDATE {table} SET status='pending', updated_at=NOW()
                WHERE status='processing' AND updated_at < NOW() - INTERVAL '10 minutes'
            """)

    wakeup: asyncio.Queue = asyncio.Queue()

    async def on_notify(conn, pid, channel, payload):
        data = json.loads(payload) if payload else {}
        await wakeup.put(data)

    listen_conn = await asyncpg.connect(settings.database_url)
    await listen_conn.add_listener("sync_jobs_queue", on_notify)

    # Process any queued sync jobs that arrived while offline
    await wakeup.put({})

    while True:
        data = await wakeup.get()
        log.info("sync_triggered", payload=data)
        async with pool.acquire() as conn:
            await run_sync(conn, settings)

asyncio.run(main())
```

### `app/domain/sync.py`

```python
import asyncio, structlog
from app.infra.ytm.client import YTMClient
from app.infra.db.repositories import PlaylistRepository, TrackRepository
from core.utils.retry import ytm_retry

log = structlog.get_logger()

async def run_sync(conn, settings):
    await conn.execute(
        "UPDATE sync_jobs SET status='running', started_at=NOW() WHERE status='queued' ORDER BY created_at LIMIT 1"
    )

    ytm = YTMClient(settings.ytm_auth_file)
    playlist_repo = PlaylistRepository(conn)
    track_repo = TrackRepository(conn)

    # 1. Fetch user playlists
    user_playlists = await fetch_library_playlists(ytm)
    await playlist_repo.upsert_many(user_playlists, playlist_type="user")

    # 2. Fetch tracks + suggested playlists (parallel, max 5 concurrent)
    semaphore = asyncio.Semaphore(5)
    suggested_ids = []

    async def process_user_playlist(pl):
        async with semaphore:
            data = await fetch_playlist(ytm, pl["id"], related=True)
            await track_repo.upsert_many(data["tracks"], track_type="user", playlist_id=pl["id"])
            related = data.get("related", [])
            await playlist_repo.upsert_many(related, playlist_type="suggested")
            suggested_ids.extend([r["id"] for r in related])
            for track in data["tracks"]:
                await track_repo.ensure_processing_rows(track["videoId"])
                await notify_track_queues(conn, track["videoId"])

    await asyncio.gather(*[process_user_playlist(pl) for pl in user_playlists], return_exceptions=True)

    # 3. Fetch suggested playlist tracks (no related expansion)
    async def process_suggested_playlist(pl_id):
        async with semaphore:
            data = await fetch_playlist(ytm, pl_id, related=False)
            await track_repo.upsert_many(data["tracks"], track_type="suggested", playlist_id=pl_id)
            for track in data["tracks"]:
                await track_repo.ensure_processing_rows(track["videoId"])
                await notify_track_queues(conn, track["videoId"])

    await asyncio.gather(*[process_suggested_playlist(pid) for pid in set(suggested_ids)], return_exceptions=True)

    await conn.execute(
        "UPDATE sync_jobs SET status='done', finished_at=NOW() WHERE status='running' ORDER BY started_at LIMIT 1"
    )
    log.info("sync_complete")

@ytm_retry
async def fetch_library_playlists(ytm: YTMClient) -> list[dict]:
    return await asyncio.get_event_loop().run_in_executor(None, ytm.get_library_playlists)

@ytm_retry
async def fetch_playlist(ytm: YTMClient, playlist_id: str, related: bool) -> dict:
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: ytm.get_playlist(playlist_id, limit=None, related=related)
    )

async def notify_track_queues(conn, video_id: str):
    import json
    payload = json.dumps({"video_id": video_id, "event": "track_added"})
    for channel in ("track_download_queue", "track_librosa_queue",
                    "track_clap_queue", "track_mert_queue"):
        await conn.execute("SELECT pg_notify($1, $2)", channel, payload)
```

### `app/infra/ytm/client.py`

```python
from ytmusicapi import YTMusic

class YTMClient:
    def __init__(self, auth_file: str):
        self.ytm = YTMusic(auth_file)

    def get_library_playlists(self) -> list[dict]:
        return self.ytm.get_library_playlists(limit=None)

    def get_playlist(self, playlist_id: str, limit=None, related=False) -> dict:
        return self.ytm.get_playlist(playlist_id, limit=limit, related=related)
```

### `app/infra/db/repositories.py`

```python
import json
import asyncpg

class PlaylistRepository:
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def upsert_many(self, playlists: list[dict], playlist_type: str):
        for pl in playlists:
            await self.conn.execute("""
                INSERT INTO yt_playlists (id, title, owner, playlist_type)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    updated_at = NOW()
            """, pl.get("playlistId") or pl.get("id"),
                 pl.get("title", ""),
                 pl.get("author", {}).get("name", ""),
                 playlist_type)

class TrackRepository:
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def upsert_many(self, tracks: list[dict], track_type: str, playlist_id: str):
        for t in tracks:
            vid = t.get("videoId")
            if not vid:
                continue
            await self.conn.execute("""
                INSERT INTO yt_tracks (id, title, artist, album, duration_seconds,
                                       track_type, source_playlist_id, metadata_json)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title, artist = EXCLUDED.artist,
                    metadata_json = EXCLUDED.metadata_json, updated_at = NOW()
            """, vid,
                 t.get("title"),
                 t.get("artists", [{}])[0].get("name"),
                 t.get("album", {}).get("name") if t.get("album") else None,
                 t.get("duration_seconds"),
                 track_type, playlist_id,
                 json.dumps(t))

    async def ensure_processing_rows(self, video_id: str):
        await self.conn.execute("""
            INSERT INTO track_downloaded (video_id, status)
            VALUES ($1, 'pending') ON CONFLICT (video_id) DO NOTHING
        """, video_id)
        for table in ("local_librosa", "local_clap", "local_mert"):
            await self.conn.execute(f"""
                INSERT INTO {table} (file_path, video_id, status)
                VALUES ($1, $2, 'pending') ON CONFLICT (file_path) DO NOTHING
            """, f"yt://{video_id}", video_id)
```

> **Note**: `local_*` rows are seeded with a placeholder `file_path` of `yt://<video_id>` before
> download. After download completes, `ytdlp-downloader` updates these rows with the real path.

## `requirements.txt`

```
asyncpg==0.29.0
ytmusicapi==1.7.0
pydantic-settings==2.3.1
structlog==24.1.0
tenacity==8.3.0
```

## `Dockerfile`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install uv --no-cache-dir
COPY core/ ./core/
COPY services/ytm_sync/requirements.txt ./requirements.txt
RUN uv pip install --system -r requirements.txt
COPY services/ytm_sync/ ./services/ytm_sync/
ENV PYTHONPATH=/app
CMD ["python", "-m", "services.ytm_sync.main"]
```

## Environment Variables

| Variable | Required | Notes |
|----------|----------|-------|
| `DATABASE_URL` | yes | |
| `YTM_AUTH_FILE` | yes | path to `secrets/ytm_auth.json` |
| `LOG_LEVEL` | no | default `INFO` |

## Suggested Playlist Expansion Limit

Hard cap: max 50 unique suggested playlist IDs per sync to avoid runaway expansion.

```python
suggested_ids = list(set(suggested_ids))[:50]
```
