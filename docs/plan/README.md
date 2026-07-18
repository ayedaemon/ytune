# YTune Engineering Blueprint

Complete plan for building YTune — a multi-service system that syncs YouTube Music, downloads
tracks, computes audio features and embeddings, and generates mood-based playlists.

## Files

| File | Contents |
|------|----------|
| [00_overview.md](00_overview.md) | Product goal, assumptions, architecture diagram, communication model, pg_notify channels |
| [01_data_model.md](01_data_model.md) | Full DB schema DDL for all tables, ER diagram, enrichment state query, idempotency patterns |
| [02_flows.md](02_flows.md) | Account sync flow, per-track processing flow, mood clustering flow, worker skeleton code |
| [03_api_spec.md](03_api_spec.md) | All REST endpoints with request/response shapes and error formats |
| [04_service_api_gateway.md](04_service_api_gateway.md) | FastAPI service — routes, domain, repositories, Dockerfile, env vars |
| [05_service_ytm_sync.md](05_service_ytm_sync.md) | YTM sync worker — ytmusicapi calls, upsert patterns, pg_notify emission, Dockerfile |
| [06_service_ytdlp_downloader.md](06_service_ytdlp_downloader.md) | yt-dlp download worker — task claiming, executor usage, path management, Dockerfile |
| [07_service_enrich_librosa.md](07_service_enrich_librosa.md) | Librosa feature extraction worker — feature list, ProcessPoolExecutor pattern, Dockerfile |
| [08_service_embeddings_clap.md](08_service_embeddings_clap.md) | CLAP embedding worker — model loading, inference, 512-dim JSONB storage, Dockerfile |
| [09_service_embeddings_mert.md](09_service_embeddings_mert.md) | MERT embedding worker — HuggingFace model, 768-dim output, Dockerfile |
| [10_service_suggestion_engine.md](10_service_suggestion_engine.md) | Clustering + similarity — k-means, FAISS index, mood_playlists schema, Dockerfile |
| [11_shared_core.md](11_shared_core.md) | Shared `core/` library — config, logging, retry configs |
| [12_docker_and_devenv.md](12_docker_and_devenv.md) | Full docker-compose.yml, .env.example, Makefile, step-by-step dev setup |
| [13_pitfalls.md](13_pitfalls.md) | 12 common mistakes and concrete fixes |

## Quick Start

```bash
# 1. Configure
cp .env.example .env
mkdir -p secrets
python scripts/extract_auth.py --browser chrome --output secrets/ytm_auth.json

# 2. Pre-download ML models (optional but avoids long first-start)
docker compose run --rm embeddings-clap python -c "import laion_clap; laion_clap.CLAP_Module().load_ckpt()"

# 3. Start
docker compose up --build

# 4. Trigger sync
curl -X POST http://localhost:8000/v1/account/sync

# 5. Watch
docker compose logs -f ytm-sync ytdlp-downloader
```

## Implementation Order

Build services in this order to validate each layer before the next:

1. `db` + migrations (schema only)
2. `api-gateway` (routes return static data first, wire DB second)
3. `ytm-sync` (upserts only, no notify yet)
4. Add pg_notify + `ytdlp-downloader`
5. `enrich-librosa`
6. `embeddings-clap`
7. `embeddings-mert`
8. `suggestion-engine`
