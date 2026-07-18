# YTune — API Specification

## Basics

- **Auth**: None for now. Add `X-API-Key` header check as a single FastAPI dependency when needed.
- **Base URL**: `http://localhost:8000/v1`
- **Versioning**: URL path (`/v1/`)
- **Content-Type**: `application/json`

## Common Error Envelope

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "Track not found",
    "details": [
      { "field": "id", "reason": "no_such_record" }
    ]
  }
}
```

Standard error codes: `VALIDATION_ERROR`, `NOT_FOUND`, `CONFLICT`, `INTERNAL_ERROR`, `SYNC_ALREADY_RUNNING`

---

## Endpoints

### `POST /v1/account/sync`

Trigger full account sync.

**Request body** (optional):
```json
{ "force": false }
```
`force: true` re-syncs even if a sync ran recently.

**Response 202**:
```json
{
  "sync_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "queued"
}
```

**Errors**: 409 (sync already running), 500

---

### `GET /v1/account/sync/status`

Get current sync state.

**Response 200**:
```json
{
  "sync_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "running",
  "started_at": "2026-07-16T10:00:00Z",
  "stats": {
    "playlists_synced": 12,
    "tracks_synced": 347,
    "tracks_errored": 2
  }
}
```

---

### `GET /v1/playlists`

List playlists with pagination.

**Query params**:
| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `type` | enum | — | `user`, `suggested`, `mood` |
| `limit` | int | 50 | max 200 |
| `cursor` | string | — | opaque pagination cursor |

**Response 200**:
```json
{
  "items": [
    {
      "id": "PLrAXtmErZgOeY0lkVCIVafdGFOOyGMGQZ",
      "title": "My Likes",
      "playlist_type": "user",
      "song_count": 142,
      "sync_state": "synced",
      "last_synced_at": "2026-07-16T09:00:00Z"
    }
  ],
  "next_cursor": "eyJpZCI6..."
}
```

---

### `GET /v1/playlists/{id}`

Get playlist detail with tracks.

**Path params**: `id` (string, YT playlist ID)

**Query params**: `limit` (int, default 50), `cursor` (string)

**Response 200**:
```json
{
  "id": "PLrAXtmErZgOeY0lkVCIVafdGFOOyGMGQZ",
  "title": "My Likes",
  "playlist_type": "user",
  "song_count": 142,
  "tracks": [
    {
      "id": "dQw4w9WgXcQ",
      "title": "Never Gonna Give You Up",
      "artist": "Rick Astley",
      "duration_seconds": 213,
      "download_status": "done",
      "librosa_status": "done",
      "clap_status": "processing",
      "mert_status": "pending"
    }
  ],
  "next_cursor": null
}
```

**Errors**: 404

---

### `GET /v1/tracks`

List tracks with enrichment filters.

**Query params**:
| Param | Type | Notes |
|-------|------|-------|
| `type` | enum | `user`, `suggested` |
| `has_download` | bool | filter by download done |
| `has_librosa` | bool | filter by librosa analysis done |
| `has_clap` | bool | filter by CLAP embedding done |
| `has_mert` | bool | filter by MERT embedding done |
| `has_errors` | bool | any enrichment table in error state |
| `q` | string | substring match (case-insensitive) against title OR artist |
| `limit` | int | default 50 |
| `cursor` | string | pagination |

**Response 200**:
```json
{
  "items": [
    {
      "id": "dQw4w9WgXcQ",
      "title": "Never Gonna Give You Up",
      "artist": "Rick Astley",
      "download_status": "done",
      "clap_status": "done",
      "mert_status": "done",
      "librosa_status": "done"
    }
  ],
  "next_cursor": null
}
```

---

### `GET /v1/tracks/{id}`

Get full track detail with optional feature vectors.

**Path params**: `id` (string, YT video ID)

**Query params**: `include_features` (bool, default false) — include raw embedding/feature vectors

**Response 200**:
```json
{
  "id": "dQw4w9WgXcQ",
  "title": "Never Gonna Give You Up",
  "artist": "Rick Astley",
  "album": "Whenever You Need Somebody",
  "duration_seconds": 213,
  "source_playlist_id": "PLrAXtmErZgOeY0lkVCIVafdGFOOyGMGQZ",
  "download": {
    "status": "done",
    "file_path": "/storage/dQw4w9WgXcQ.opus"
  },
  "librosa": {
    "status": "done",
    "features": { "tempo": 113.4, "key": 5, "energy": 0.78 }
  },
  "clap": {
    "status": "done",
    "embedding": null
  },
  "mert": {
    "status": "done",
    "embedding": null
  }
}
```
`embedding` is `null` unless `include_features=true`.

**Errors**: 404

---

### `GET /v1/mood/playlists`

List mood-based playlists from suggestion engine.

**Response 200**:
```json
{
  "items": [
    {
      "id": 1,
      "label": "energetic",
      "track_count": 34,
      "updated_at": "2026-07-16T08:00:00Z"
    }
  ]
}
```

---

### `GET /v1/mood/playlists/{id}/tracks`

Get tracks in a mood playlist.

**Path params**: `id` (int, mood_playlist id)

**Query params**: `limit` (int, default 50), `cursor` (string)

**Response 200**:
```json
{
  "mood_playlist": { "id": 1, "label": "energetic" },
  "tracks": [
    {
      "id": "dQw4w9WgXcQ",
      "title": "Never Gonna Give You Up",
      "score": 0.92
    }
  ],
  "next_cursor": null
}
```

---

### `POST /v1/suggestion/similar`

Find similar tracks by embedding distance.

**Request body**:
```json
{
  "video_id": "dQw4w9WgXcQ",
  "top_k": 10,
  "model": "clap"
}
```
`model`: `clap` (default) or `mert`

**Response 200**:
```json
{
  "query_id": "dQw4w9WgXcQ",
  "results": [
    { "id": "abc123", "title": "Uptown Funk", "similarity": 0.91 }
  ]
}
```

**Errors**: 404 (track not found or no embedding available)

---

### `POST /v1/suggestion/recompute`

Force re-run of mood clustering.

**Response 202**:
```json
{ "status": "queued" }
```

---

### `GET /health`

Health check (each service exposes this).

**Response 200**:
```json
{ "status": "ok", "service": "api-gateway" }
```
