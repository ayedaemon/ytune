# Common Pitfalls & How to Avoid Them

## 1. Forgetting migrations after schema changes

**Symptom**: `UndefinedTableError` or `column "X" of relation "Y" does not exist`.

**Fix**: Add a numbered file to `db/migrations/` for every schema change. Run `make migrate` after
every `git pull`. The `migrate` service in docker-compose runs all `*.sql` files in alphabetical
order on startup.

```bash
# Never forget:
make migrate
docker compose restart  # restart services to pick up new schema
```

---

## 2. Tasks stuck in `processing` state after worker crash

**Symptom**: Rows in `status = 'processing'` that never progress. New tracks ignored because
they conflict with "processing" rows.

**Fix**: Every worker runs this on startup:

```sql
UPDATE track_downloaded
SET status = 'pending', updated_at = NOW()
WHERE status = 'processing'
  AND updated_at < NOW() - INTERVAL '10 minutes';
```

Apply to all tables: `local_librosa`, `local_clap`, `local_mert`.

---

## 3. Race condition — two workers claim same task

**Symptom**: Same track downloaded twice, duplicate embeddings, conflicting DB updates.

**Fix**: Always use `SELECT ... FOR UPDATE SKIP LOCKED` inside a transaction. Never do
check-then-update at the application level.

```sql
-- WRONG (race condition):
SELECT id FROM track_downloaded WHERE status = 'pending' LIMIT 1;
UPDATE track_downloaded SET status = 'processing' WHERE id = $1;

-- CORRECT:
BEGIN;
SELECT id FROM track_downloaded WHERE status = 'pending' LIMIT 1 FOR UPDATE SKIP LOCKED;
UPDATE track_downloaded SET status = 'processing' WHERE id = $1;
COMMIT;
```

---

## 4. Blocking the event loop with CPU-heavy work

**Symptom**: All asyncio tasks freeze while one track is being processed. `asyncio` warns
about slow coroutines. Services become unresponsive.

**Fix**: librosa, CLAP inference, MERT inference are synchronous CPU work. Always wrap:

```python
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(ProcessPoolExecutor(max_workers=2), sync_fn, arg)
```

Never call `librosa.load()`, `model.embed()` etc. directly inside an `async def`.

---

## 5. pg_notify payload over 8000 bytes

**Symptom**: `ERROR: payload string too long` from PostgreSQL.

**Fix**: Keep notify payloads minimal. Only pass `video_id` and `event` name. Never embed
embeddings, feature vectors, or large metadata in the payload. Workers always query the DB
for full data after receiving a notification.

```python
# WRONG:
payload = json.dumps({"video_id": vid, "embedding": [...512 floats...]})

# CORRECT:
payload = json.dumps({"video_id": vid, "event": "track_added"})
```

---

## 6. YTM auth cookies expiring silently

**Symptom**: `ytm-sync` returns empty playlists or raises auth exceptions after a few days.
Sync job status shows `done` but track count is 0.

**Fix**:
- Catch auth errors in the sync loop, set `sync_jobs.status = 'error'` with `error = "auth_expired"`.
- Surface this via `GET /v1/account/sync/status`.
- Re-run `python scripts/extract_auth.py` to refresh cookies.

---

## 7. Suggested playlist expansion blowing up

**Symptom**: ytm-sync fetches thousands of suggested playlists, sync never completes, DB grows
unbounded.

**Fix**: Hard cap at 50 unique suggested playlist IDs per sync:

```python
suggested_ids = list(set(suggested_ids))[:50]
```

Only expand `related=True` for **user** playlists. When processing suggested playlists, always
use `related=False`.

---

## 8. Partial enrichment failure not surfaced

**Symptom**: Track shows `download_status: done` but `clap_status: error`. API returns no
indication. User sees tracks in mood playlists that were actually never embedded.

**Fix**:
- `GET /v1/tracks/{id}` must always include all four status fields.
- Add `GET /v1/tracks?has_errors=true` filter to find broken tracks.
- Suggestion engine's `JOIN` query naturally excludes tracks without `status = 'done'` in all
  three enrichment tables — they simply won't appear in mood playlists.

---

## 9. Large embeddings causing slow queries and high storage

**Symptom**: `local_clap` and `local_mert` tables grow to GB. `SELECT *` queries are slow.
Similarity computations time out.

**Fix**:
- Never `SELECT *` from `local_clap` or `local_mert` in API handlers. Only select `status`.
- For similarity search, migrate to `pgvector` extension:
  ```sql
  ALTER TABLE local_clap ADD COLUMN embedding_vec vector(512);
  CREATE INDEX ON local_clap USING ivfflat (embedding_vec vector_cosine_ops) WITH (lists = 100);
  ```
- Pre-compute top-N similar tracks and store in `track_similarity` table (see suggestion-engine).

---

## 10. YTM rate limiting (429 / IP bans)

**Symptom**: ytmusicapi raises exceptions after many rapid requests. IP temporarily blocked.

**Fix**:
- Add `asyncio.sleep(0.5)` between playlist fetches.
- Cap parallel playlist fetches at `Semaphore(5)`.
- Use `ytm_retry` with exponential backoff (already in `core/utils/retry.py`).
- Do not re-sync more frequently than every 30 minutes.

---

## 11. `yt://` placeholder paths picked up by enrichment workers

**Symptom**: Librosa/CLAP/MERT workers pick up rows where `file_path = 'yt://dQw4w9WgXcQ'`
and fail immediately because that path doesn't exist on disk.

**Fix**: All enrichment worker `claim_pending` queries include:

```sql
WHERE file_path IS NOT NULL AND file_path NOT LIKE 'yt://%'
```

Workers that pick up a placeholder due to a race condition should call `release()` to put
the row back to `pending` rather than marking it as an error.

---

## 12. First `docker compose up` downloads 10GB of ML models

**Symptom**: Service containers fail to start or hang for 30+ minutes waiting for model downloads.

**Fix**: Pre-download models into the `model_cache` Docker volume before starting the full
stack:

```bash
docker compose run --rm embeddings-clap python -c "import laion_clap; laion_clap.CLAP_Module().load_ckpt()"
docker compose run --rm embeddings-mert python -c "
from transformers import AutoModel
AutoModel.from_pretrained('m-a-p/MERT-v1-95M', trust_remote_code=True, cache_dir='/models/mert')
"
docker compose up  # now starts fast
```

Or add a `model-downloader` service with `restart: "no"` that other ML services `depend_on`.
