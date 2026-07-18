# YTune — System Flows

## 3.1 Account Sync Flow

```
Client                api-gateway             DB               ytm-sync
  │                       │                   │                    │
  │  POST /v1/account/sync│                   │                    │
  │──────────────────────▶│                   │                    │
  │                       │  INSERT sync_jobs │                    │
  │                       │──────────────────▶│                    │
  │                       │  NOTIFY sync_jobs_queue               │
  │                       │──────────────────▶│                    │
  │  202 {sync_id, status}│                   │                    │
  │◀──────────────────────│                   │  LISTEN fires      │
  │                       │                   │───────────────────▶│
  │                       │                   │                    │ ytm.get_library_playlists()
  │                       │                   │                    │ UPSERT yt_playlists (user)
  │                       │                   │◀───────────────────│
  │                       │                   │                    │ for each user playlist:
  │                       │                   │                    │   ytm.get_playlist(id, limit=None, related=True)
  │                       │                   │                    │   UPSERT yt_tracks (user)
  │                       │                   │                    │   UPSERT yt_playlists from related (suggested)
  │                       │                   │                    │   NOTIFY track_download_queue
  │                       │                   │                    │   NOTIFY track_librosa_queue
  │                       │                   │                    │   NOTIFY track_clap_queue
  │                       │                   │                    │   NOTIFY track_mert_queue
  │                       │                   │                    │ for each suggested playlist:
  │                       │                   │                    │   ytm.get_playlist(id, limit=None, related=False)
  │                       │                   │                    │   UPSERT yt_tracks (suggested)
  │                       │                   │                    │   NOTIFY (same 4 channels)
  │                       │                   │                    │ UPDATE sync_jobs SET status='done'
```

### Upsert Pattern

```python
await conn.execute("""
    INSERT INTO yt_tracks (id, title, artist, album, duration_seconds,
                           track_type, source_playlist_id, metadata_json)
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
    ON CONFLICT (id) DO UPDATE SET
        title             = EXCLUDED.title,
        artist            = EXCLUDED.artist,
        album             = EXCLUDED.album,
        duration_seconds  = EXCLUDED.duration_seconds,
        metadata_json     = EXCLUDED.metadata_json,
        updated_at        = NOW()
""", video_id, title, artist, album, duration, track_type,
     playlist_id, json.dumps(raw_metadata))
```

### Ensure Processing Rows Exist

Called after each track upsert:

```python
async def ensure_processing_rows(conn, video_id: str, file_path: str | None):
    await conn.execute("""
        INSERT INTO track_downloaded (video_id, status)
        VALUES ($1, 'pending')
        ON CONFLICT (video_id) DO NOTHING
    """, video_id)

    for table in ("local_librosa", "local_clap", "local_mert"):
        if file_path:
            await conn.execute(f"""
                INSERT INTO {table} (file_path, video_id, status)
                VALUES ($1, $2, 'pending')
                ON CONFLICT (file_path) DO NOTHING
            """, file_path, video_id)
```

### Emit Notify

```python
import json

async def notify_track_queues(conn, video_id: str):
    payload = json.dumps({"video_id": video_id, "event": "track_added"})
    for channel in ("track_download_queue", "track_librosa_queue",
                    "track_clap_queue", "track_mert_queue"):
        await conn.execute("SELECT pg_notify($1, $2)", channel, payload)
```

---

## 3.2 Per-Track Processing Flow

```
ytm-sync
  │
  │  INSERT track_downloaded(video_id, status='pending')
  │  INSERT local_librosa / local_clap / local_mert (status='pending')
  │  NOTIFY 'track_download_queue' '{"video_id":"<id>"}'
  │  NOTIFY 'track_librosa_queue'  '{"video_id":"<id>"}'
  │  NOTIFY 'track_clap_queue'     '{"video_id":"<id>"}'
  │  NOTIFY 'track_mert_queue'     '{"video_id":"<id>"}'
  │
  ▼
ytdlp-downloader (LISTEN track_download_queue)
  │
  │  SELECT id, video_id FROM track_downloaded
  │     WHERE status = 'pending'
  │     ORDER BY created_at
  │     LIMIT 5
  │     FOR UPDATE SKIP LOCKED
  │
  │  UPDATE status = 'downloading'
  │  yt-dlp download → file_path
  │  UPDATE status = 'done', file_path = <path>
  │  UPDATE local_librosa/clap/mert SET file_path = <path>
  │         WHERE video_id = <id> AND file_path IS NULL
  │
  ▼
enrich-librosa / embeddings-clap / embeddings-mert
  (same LISTEN → SELECT FOR UPDATE SKIP LOCKED → process → update pattern)
  │
  │  After each done update, check threshold:
  │  SELECT COUNT(*) FROM yt_tracks t
  │    JOIN local_clap    lc ON lc.video_id = t.id AND lc.status = 'done'
  │    JOIN local_mert    lm ON lm.video_id = t.id AND lm.status = 'done'
  │    JOIN local_librosa ll ON ll.video_id = t.id AND ll.status = 'done'
  │  WHERE t.updated_at > (last_cluster_run)
  │
  │  If count >= CLUSTER_TRIGGER_THRESHOLD:
  │    NOTIFY 'suggestion_engine_queue' '{}'
```

### Generic Worker Listener Skeleton

Reusable across all workers:

```python
import asyncpg, asyncio, json

async def worker_loop(dsn: str, channel: str, process_pending):
    conn = await asyncpg.connect(dsn)
    queue: asyncio.Queue = asyncio.Queue()

    async def handle_notify(con, pid, ch, payload):
        await queue.put(json.loads(payload) if payload else {})

    await conn.add_listener(channel, handle_notify)

    # drain any pending tasks missed while offline
    await queue.put({})

    while True:
        await queue.get()
        await process_pending(conn)
```

### Claim Tasks (FOR UPDATE SKIP LOCKED)

```python
async def claim_pending(conn, table: str, limit: int = 5) -> list:
    async with conn.transaction():
        rows = await conn.fetch(f"""
            SELECT id, video_id, file_path
            FROM {table}
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT {limit}
            FOR UPDATE SKIP LOCKED
        """)
        if rows:
            ids = [r["id"] for r in rows]
            await conn.execute(
                f"UPDATE {table} SET status='processing', updated_at=NOW() WHERE id = ANY($1)",
                ids
            )
        return list(rows)
```

---

## 3.3 Mood Clustering & Discovery Flow

**Trigger conditions**:
- `NOTIFY suggestion_engine_queue` when `CLUSTER_TRIGGER_THRESHOLD` new tracks finish all enrichment (default: 10)
- Manual `POST /v1/suggestion/recompute`
- Optional cron every 6 hours

**Algorithm**:

```
suggestion-engine
  │
  │  SELECT t.id, lc.embedding, lm.embedding, ll.features
  │  FROM yt_tracks t
  │  JOIN local_clap    lc ON lc.video_id = t.id AND lc.status = 'done'
  │  JOIN local_mert    lm ON lm.video_id = t.id AND lm.status = 'done'
  │  JOIN local_librosa ll ON ll.video_id = t.id AND ll.status = 'done'
  │
  │  Concat feature vectors per track
  │  Run k-means (sklearn, k = CLUSTER_K env var, default 8)
  │  Assign cluster labels → mood names (predefined map or LLM labeling)
  │
  │  UPSERT mood_playlists (label, cluster_id, track_count)
  │  UPSERT mood_track_map (mood_playlist_id, video_id, score)
  │
  │  For similarity index:
  │    Build FAISS flat index over CLAP embeddings
  │    Serialize to file (or store top-N similar per track in DB)
  │
  │  Log completion
```

**Incremental recompute**: Track `last_cluster_run` timestamp. Only re-cluster tracks updated after
that timestamp. Merge with existing cluster assignments rather than full recompute each time.

**API surface**:
- `GET /v1/mood/playlists` — returns `mood_playlists` rows
- `GET /v1/mood/playlists/{id}/tracks` — returns tracks in cluster
- `POST /v1/suggestion/similar` — `{video_id, top_k}`, returns nearest neighbors by CLAP cosine similarity
