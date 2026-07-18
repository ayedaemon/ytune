# Service: suggestion-engine

## Purpose

Listens on `suggestion_engine_queue`. When triggered:
1. Loads all tracks with completed enrichment (librosa + CLAP + MERT)
2. Concatenates feature vectors per track
3. Runs k-means clustering → mood-based playlists
4. Builds FAISS similarity index over CLAP embeddings
5. Writes `mood_playlists` and `mood_track_map` tables

Also serves similarity queries via a DB-backed top-N pre-computation or in-memory FAISS index.

## Trigger Conditions

| Condition | How |
|-----------|-----|
| N new fully-enriched tracks (default N=10) | `NOTIFY suggestion_engine_queue` from any enrichment worker after update |
| Manual force | `POST /v1/suggestion/recompute` → api-gateway → `NOTIFY suggestion_engine_queue` |
| Optional cron | Simple `asyncio.sleep(21600)` loop in addition to notify listener |

## Directory Layout

```
services/suggestion_engine/
├── Dockerfile
├── requirements.txt
├── main.py
└── app/
    ├── domain/
    │   ├── clustering.py           # k-means, cluster labeling
    │   └── recommendations.py     # FAISS index, top-N similarity
    └── infra/
        └── db/
            └── repositories.py
```

## Key Files

### `main.py`

```python
import asyncio, asyncpg
from core.config import SuggestionSettings
from core.logging import configure_logging
from app.domain.clustering import run_clustering
from app.domain.recommendations import build_similarity_index

settings = SuggestionSettings()
configure_logging("suggestion-engine", settings.log_level)

async def main():
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)

    wakeup: asyncio.Queue = asyncio.Queue()

    async def on_notify(conn, pid, channel, payload):
        await wakeup.put({})

    listen_conn = await asyncpg.connect(settings.database_url)
    await listen_conn.add_listener("suggestion_engine_queue", on_notify)

    while True:
        await wakeup.get()
        async with pool.acquire() as conn:
            await run_clustering(conn, settings)
            await build_similarity_index(conn, settings)

asyncio.run(main())
```

### `app/domain/clustering.py`

```python
import json, structlog
import numpy as np
from sklearn.cluster import KMeans
from app.infra.db.repositories import SuggestionRepository

log = structlog.get_logger()

MOOD_LABELS = {
    0: "energetic",   1: "melancholic",  2: "chill",     3: "upbeat",
    4: "tense",       5: "romantic",     6: "focus",     7: "experimental",
}

async def run_clustering(conn, settings):
    repo = SuggestionRepository(conn)
    rows = await repo.load_enriched_tracks()

    if len(rows) < int(settings.cluster_k):
        log.warning("too_few_tracks_for_clustering", n=len(rows), k=settings.cluster_k)
        return

    track_ids = []
    feature_matrix = []

    for row in rows:
        clap_emb = json.loads(row["clap_embedding"])   # 512 floats
        mert_emb = json.loads(row["mert_embedding"])   # 768 floats
        librosa_f = json.loads(row["librosa_features"])
        # Simple feature: concat normalized embeddings + key scalar features
        scalar = [
            librosa_f.get("tempo", 0) / 200.0,
            librosa_f.get("energy", librosa_f.get("rms_mean", 0)),
            librosa_f.get("key", 0) / 11.0,
        ]
        vec = clap_emb + mert_emb + scalar
        track_ids.append(row["video_id"])
        feature_matrix.append(vec)

    X = np.array(feature_matrix, dtype=np.float32)
    k = int(settings.cluster_k)
    kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
    labels = kmeans.fit_predict(X)
    scores = kmeans.transform(X).min(axis=1)  # distance to assigned centroid

    # Normalize to [0,1] confidence (1 = closest)
    max_dist = scores.max() or 1.0
    confidences = 1.0 - (scores / max_dist)

    # Upsert mood playlists and track map
    for cluster_id in range(k):
        label = MOOD_LABELS.get(cluster_id, f"cluster_{cluster_id}")
        mood_id = await repo.upsert_mood_playlist(label, cluster_id)
        mask = labels == cluster_id
        cluster_track_ids = [track_ids[i] for i in range(len(track_ids)) if mask[i]]
        cluster_scores    = [float(confidences[i]) for i in range(len(track_ids)) if mask[i]]
        await repo.upsert_mood_track_map(mood_id, cluster_track_ids, cluster_scores)
        await repo.update_track_count(mood_id, len(cluster_track_ids))

    log.info("clustering_done", n_tracks=len(track_ids), k=k)
```

### `app/domain/recommendations.py`

```python
import json, structlog
import numpy as np
from app.infra.db.repositories import SuggestionRepository

log = structlog.get_logger()

async def build_similarity_index(conn, settings):
    """
    Compute and store top-20 similar tracks per track by CLAP cosine similarity.
    Stored in a `track_similarity` table (add to schema if needed).
    Alternative: build FAISS index in memory for real-time queries.
    """
    try:
        import faiss
    except ImportError:
        log.warning("faiss_not_installed_skipping_similarity")
        return

    repo = SuggestionRepository(conn)
    rows = await repo.load_clap_embeddings()

    if not rows:
        return

    track_ids = [r["video_id"] for r in rows]
    embeddings = np.array([json.loads(r["embedding"]) for r in rows], dtype=np.float32)

    # L2 normalize for cosine similarity
    faiss.normalize_L2(embeddings)

    index = faiss.IndexFlatIP(embeddings.shape[1])  # inner product = cosine after normalization
    index.add(embeddings)

    top_k = 20
    D, I = index.search(embeddings, top_k + 1)  # +1 because first result is self

    for i, video_id in enumerate(track_ids):
        similar = []
        for rank in range(1, top_k + 1):  # skip index 0 (self)
            neighbor_idx = I[i][rank]
            if neighbor_idx == -1:
                break
            similar.append({
                "video_id": track_ids[neighbor_idx],
                "score": float(D[i][rank])
            })
        await repo.upsert_similar_tracks(video_id, similar)

    log.info("similarity_index_built", n_tracks=len(track_ids))
```

### `app/infra/db/repositories.py`

```python
import json
import asyncpg

class SuggestionRepository:
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def load_enriched_tracks(self) -> list:
        return await self.conn.fetch("""
            SELECT t.id AS video_id,
                   lc.embedding AS clap_embedding,
                   lm.embedding AS mert_embedding,
                   ll.features  AS librosa_features
            FROM yt_tracks t
            JOIN local_clap    lc ON lc.video_id = t.id AND lc.status = 'done'
            JOIN local_mert    lm ON lm.video_id = t.id AND lm.status = 'done'
            JOIN local_librosa ll ON ll.video_id = t.id AND ll.status = 'done'
        """)

    async def load_clap_embeddings(self) -> list:
        return await self.conn.fetch("""
            SELECT video_id, embedding FROM local_clap WHERE status = 'done'
        """)

    async def upsert_mood_playlist(self, label: str, cluster_id: int) -> int:
        return await self.conn.fetchval("""
            INSERT INTO mood_playlists (label, cluster_id)
            VALUES ($1, $2)
            ON CONFLICT (cluster_id) DO UPDATE SET label=EXCLUDED.label, updated_at=NOW()
            RETURNING id
        """, label, cluster_id)

    async def upsert_mood_track_map(self, mood_id: int, track_ids: list, scores: list):
        # Delete old assignments for this mood playlist, then re-insert
        await self.conn.execute("DELETE FROM mood_track_map WHERE mood_playlist_id=$1", mood_id)
        await self.conn.executemany("""
            INSERT INTO mood_track_map (mood_playlist_id, video_id, score) VALUES ($1,$2,$3)
        """, [(mood_id, vid, score) for vid, score in zip(track_ids, scores)])

    async def update_track_count(self, mood_id: int, count: int):
        await self.conn.execute(
            "UPDATE mood_playlists SET track_count=$1, updated_at=NOW() WHERE id=$2", count, mood_id
        )

    async def upsert_similar_tracks(self, video_id: str, similar: list[dict]):
        # Requires a track_similarity table:
        # CREATE TABLE track_similarity (
        #   video_id TEXT, similar_video_id TEXT, score FLOAT,
        #   PRIMARY KEY (video_id, similar_video_id)
        # );
        await self.conn.execute(
            "DELETE FROM track_similarity WHERE video_id=$1", video_id
        )
        await self.conn.executemany("""
            INSERT INTO track_similarity (video_id, similar_video_id, score) VALUES ($1,$2,$3)
        """, [(video_id, s["video_id"], s["score"]) for s in similar])
```

## Additional Schema (add to migrations)

```sql
CREATE TABLE track_similarity (
    video_id         TEXT REFERENCES yt_tracks(id) ON DELETE CASCADE,
    similar_video_id TEXT REFERENCES yt_tracks(id) ON DELETE CASCADE,
    score            FLOAT NOT NULL,
    PRIMARY KEY (video_id, similar_video_id)
);

-- Need unique constraint on mood_playlists for ON CONFLICT
ALTER TABLE mood_playlists ADD CONSTRAINT uq_cluster_id UNIQUE (cluster_id);
```

## `requirements.txt`

```
asyncpg==0.29.0
scikit-learn==1.5.0
numpy==1.26.4
faiss-cpu==1.8.0       # or faiss-gpu if CUDA available
pydantic-settings==2.3.1
structlog==24.1.0
```

## `Dockerfile`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install uv --no-cache-dir
COPY core/ ./core/
COPY services/suggestion_engine/requirements.txt ./requirements.txt
RUN uv pip install --system -r requirements.txt
COPY services/suggestion_engine/ ./services/suggestion_engine/
ENV PYTHONPATH=/app
CMD ["python", "-m", "services.suggestion_engine.main"]
```

## Environment Variables

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `DATABASE_URL` | yes | — | |
| `CLUSTER_K` | no | `8` | number of mood clusters |
| `CLUSTER_TRIGGER_THRESHOLD` | no | `10` | new enriched tracks before re-cluster |
| `LOG_LEVEL` | no | `INFO` | |

## Notes

- k-means is deterministic given `random_state=42` and same data — safe to re-run.
- Incremental clustering: only re-cluster if new tracks since `last_cluster_run`. Add a
  `cluster_runs` table or a simple file timestamp to track this.
- If FAISS is not installed, similarity index is skipped gracefully (warning logged).
- `mood_track_map` is fully replaced on each cluster run — no incremental merge needed
  since k-means assigns all tracks, not just new ones.
