# YTune Engineering Blueprint — Overview

## Product Goal

YTune syncs a user's YouTube Music (YTM) library into a local PostgreSQL database, downloads audio
files, and enriches them with acoustic features (librosa) and neural embeddings (CLAP, MERT). These
enrichments feed a suggestion engine that clusters tracks into mood-based playlists and surfaces
discovery recommendations. The entire system runs on a single Linux host via Docker Compose, with
services coordinating through PostgreSQL and `pg_notify` rather than a message broker. The user
interacts through a REST API; all heavy CPU/IO work happens in dedicated background workers.

## Explicit Assumptions

| # | Assumption |
|---|-----------|
| 1 | Single primary YTM account. Schema includes `owner` columns so multi-user extension is straightforward later. |
| 2 | YTM is the authoritative source for playlist/track metadata. Local DB is a mirror + enrichment layer. |
| 3 | Local audio files (non-YTM) are supported as first-class citizens in `local_*` tables via `file_path`; `video_id` is nullable for them. |
| 4 | Single Linux host, Docker Compose. Horizontal scaling is possible later by pointing multiple containers at the same DB. |
| 5 | No auth on API for now (single user). Auth hooks noted where they'd plug in. |
| 6 | GPU optional. All ML services fall back to CPU if no CUDA device detected. |
| 7 | `uv` manages per-service Python deps via `requirements.txt`. No global lock required. |

## Resolved Ambiguities

| Ambiguity | Resolution |
|-----------|-----------|
| How does api-gateway trigger ytm-sync? | Insert row into `sync_jobs` + `pg_notify`. No direct HTTP between internal services. |
| Where do suggested tracks come from? | From the `related` field of `ytm.get_playlist()` on *user* playlists only. Suggested playlists then fetched without `related=True`. |
| Embedding storage format | JSONB for simplicity. Migrate to `pgvector` `vector(N)` column when similarity queries are needed. |
| Mood clustering trigger | Event-driven: after N new tracks reach `done` across all enrichment tables. Also supports manual `POST /v1/suggestion/recompute`. |
| `candidate_playlist_ids` shape | JSONB array of `{id, title}` objects on `yt_playlists`. Normalize if individual ID queries are needed. |

## Service Map

| Service | Role |
|---------|------|
| `api-gateway` | User-facing FastAPI HTTP API |
| `ytm-sync` | Calls ytmusicapi, upserts DB, emits pg_notify |
| `ytdlp-downloader` | Downloads audio via yt-dlp |
| `enrich-librosa` | Runs librosa feature extraction |
| `embeddings-clap` | Computes CLAP embeddings |
| `embeddings-mert` | Computes MERT embeddings |
| `suggestion-engine` | Clusters tracks into mood playlists, computes similarity |

## Architecture Diagram

```text
                   ┌──────────────────────────────────────────────────┐
                   │                  PostgreSQL                       │
                   │  yt_playlists  yt_tracks  track_downloaded        │
                   │  local_clap  local_mert  local_librosa            │
                   │  sync_jobs  mood_playlists  mood_track_map        │
                   └────────────────────┬─────────────────────────────┘
                                        │ pg_notify channels
     ┌────────────┐    REST    ┌─────────┴──────────┐
     │   Client   │──────────▶│    api-gateway      │
     └────────────┘           │  (FastAPI :8000)    │
                              └─────────┬───────────┘
                                        │ INSERT sync_jobs + NOTIFY
                              ┌─────────▼───────────┐
                              │      ytm-sync        │
                              │  (ytmusicapi calls)  │
                              └─────────┬────────────┘
                                        │ NOTIFY (4 channels)
                  ┌─────────────────────┼───────────────────────┐
                  │                     │                        │
       ┌──────────▼──────┐  ┌───────────▼──────────┐  ┌─────────▼──────────┐
       │ ytdlp-downloader│  │   enrich-librosa      │  │  embeddings-clap   │
       └──────────┬──────┘  └───────────┬───────────┘  └─────────┬──────────┘
                  │                     │                         │
       ┌──────────▼─────────────────────▼─────────────────────────▼──────────┐
       │                     PostgreSQL (status updates)                      │
       └──────────────────────────────────┬───────────────────────────────────┘
                                          │ NOTIFY suggestion_engine_queue
                              ┌───────────▼──────────┐
                              │  suggestion-engine   │
                              │  (clustering + recs) │
                              └──────────────────────┘

  embeddings-mert follows same pattern as embeddings-clap (omitted for brevity)
```

## Communication Model

- **Client → api-gateway**: HTTP/REST
- **api-gateway → ytm-sync**: `INSERT INTO sync_jobs` + `NOTIFY sync_jobs_queue`
- **ytm-sync → workers**: `NOTIFY <channel>` after upserting each track
- **Workers → DB**: Direct asyncpg writes for status updates
- **Workers → suggestion-engine**: After N tracks complete enrichment, notify `suggestion_engine_queue`

**Why DB + pg_notify instead of Kafka/RabbitMQ**:
- No extra infrastructure
- DB is single source of truth; notify is just a wake-up call
- Crash-safe: missed notifications recovered by startup DB scan
- Sufficient throughput for single-host, single-user workload

## pg_notify Channel Registry

| Channel | Producer | Consumer |
|---------|----------|---------|
| `sync_jobs_queue` | api-gateway | ytm-sync |
| `track_download_queue` | ytm-sync | ytdlp-downloader |
| `track_librosa_queue` | ytm-sync | enrich-librosa |
| `track_clap_queue` | ytm-sync | embeddings-clap |
| `track_mert_queue` | ytm-sync | embeddings-mert |
| `suggestion_engine_queue` | ytdlp-downloader (after N done) | suggestion-engine |

## Error Handling Strategy

```
Worker claims row (status = processing)
  ├─ success: status = done
  └─ exception:
       ├─ retries < MAX_RETRIES: status = pending, retries++, sleep(backoff)
       └─ retries >= MAX_RETRIES: status = error, error_message = str(exc)
```

On every worker startup, recover stuck rows:
```sql
UPDATE <table>
SET status = 'pending', updated_at = NOW()
WHERE status = 'processing'
  AND updated_at < NOW() - INTERVAL '10 minutes';
```
