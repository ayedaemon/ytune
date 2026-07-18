# Docker & Local Development Setup

## `.env.example`

```bash
# ── Database ──────────────────────────────────────────────
DATABASE_URL=postgresql://ytune:ytune@db:5432/ytune
DB_MAX_POOL_SIZE=10

# ── Storage ───────────────────────────────────────────────
STORAGE_ROOT=/storage

# ── YTM Auth ──────────────────────────────────────────────
YTM_AUTH_FILE=/secrets/ytm_auth.json

# ── Logging ───────────────────────────────────────────────
LOG_LEVEL=INFO

# ── Worker concurrency ────────────────────────────────────
MAX_CONCURRENT_DOWNLOADS=3
MAX_CONCURRENT_WORKERS=2

# ── ML Models ─────────────────────────────────────────────
CLAP_MODEL_PATH=/models/clap
MERT_MODEL_PATH=/models/mert
DEVICE=cpu

# ── Suggestion Engine ─────────────────────────────────────
CLUSTER_K=8
CLUSTER_TRIGGER_THRESHOLD=10
```

## `docker-compose.yml`

```yaml
version: "3.9"

x-common-env: &common-env
  DATABASE_URL: postgresql://ytune:ytune@db:5432/ytune
  STORAGE_ROOT: /storage
  LOG_LEVEL: ${LOG_LEVEL:-INFO}
  PYTHONPATH: /app

services:

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: ytune
      POSTGRES_PASSWORD: ytune
      POSTGRES_DB: ytune
    volumes:
      - pg_data:/var/lib/postgresql/data
      - ./db/init:/docker-entrypoint-initdb.d:ro
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ytune -d ytune"]
      interval: 5s
      timeout: 5s
      retries: 10

  migrate:
    image: postgres:16-alpine
    depends_on:
      db:
        condition: service_healthy
    volumes:
      - ./db/migrations:/migrations:ro
    environment:
      PGPASSWORD: ytune
    entrypoint: >
      sh -c "for f in /migrations/*.sql; do
               echo \"Running $$f\";
               psql -h db -U ytune -d ytune -f $$f;
             done && echo 'Migrations done'"
    restart: "no"

  api-gateway:
    build:
      context: .
      dockerfile: services/api_gateway/Dockerfile
    environment:
      <<: *common-env
      DB_MAX_POOL_SIZE: ${DB_MAX_POOL_SIZE:-10}
    ports:
      - "8000:8000"
    volumes:
      - track_storage:/storage:ro
    depends_on:
      migrate:
        condition: service_completed_successfully
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 5

  ytm-sync:
    build:
      context: .
      dockerfile: services/ytm_sync/Dockerfile
    environment:
      <<: *common-env
      YTM_AUTH_FILE: /secrets/ytm_auth.json
    volumes:
      - ./secrets:/secrets:ro
    depends_on:
      migrate:
        condition: service_completed_successfully

  ytdlp-downloader:
    build:
      context: .
      dockerfile: services/ytdlp_downloader/Dockerfile
    environment:
      <<: *common-env
      MAX_CONCURRENT_DOWNLOADS: ${MAX_CONCURRENT_DOWNLOADS:-3}
    volumes:
      - track_storage:/storage
    depends_on:
      migrate:
        condition: service_completed_successfully

  enrich-librosa:
    build:
      context: .
      dockerfile: services/enrich_librosa/Dockerfile
    environment:
      <<: *common-env
      MAX_CONCURRENT_WORKERS: ${MAX_CONCURRENT_WORKERS:-2}
    volumes:
      - track_storage:/storage:ro
    depends_on:
      migrate:
        condition: service_completed_successfully

  embeddings-clap:
    build:
      context: .
      dockerfile: services/embeddings_clap/Dockerfile
    environment:
      <<: *common-env
      CLAP_MODEL_PATH: ${CLAP_MODEL_PATH:-/models/clap}
      DEVICE: ${DEVICE:-cpu}
      MAX_CONCURRENT_WORKERS: ${MAX_CONCURRENT_WORKERS:-1}
    volumes:
      - track_storage:/storage:ro
      - model_cache:/models
    depends_on:
      migrate:
        condition: service_completed_successfully

  embeddings-mert:
    build:
      context: .
      dockerfile: services/embeddings_mert/Dockerfile
    environment:
      <<: *common-env
      MERT_MODEL_PATH: ${MERT_MODEL_PATH:-/models/mert}
      DEVICE: ${DEVICE:-cpu}
      MAX_CONCURRENT_WORKERS: ${MAX_CONCURRENT_WORKERS:-1}
    volumes:
      - track_storage:/storage:ro
      - model_cache:/models
    depends_on:
      migrate:
        condition: service_completed_successfully

  suggestion-engine:
    build:
      context: .
      dockerfile: services/suggestion_engine/Dockerfile
    environment:
      <<: *common-env
      CLUSTER_K: ${CLUSTER_K:-8}
      CLUSTER_TRIGGER_THRESHOLD: ${CLUSTER_TRIGGER_THRESHOLD:-10}
    depends_on:
      migrate:
        condition: service_completed_successfully

volumes:
  pg_data:
  track_storage:
  model_cache:
```

## Local Dev Workflow

### Step 1 — Clone and configure

```bash
git clone https://github.com/ayedaemon/ytune
cd ytune
cp .env.example .env
mkdir -p secrets
```

### Step 2 — Extract YTM auth (run on host, not in Docker)

Chrome must be open and logged into YouTube Music:

```bash
pip install browser-cookie3 ytmusicapi
python scripts/extract_auth.py --browser chrome --output secrets/ytm_auth.json
```

Verify:
```bash
python -c "from ytmusicapi import YTMusic; ytm = YTMusic('secrets/ytm_auth.json'); print(ytm.get_library_playlists(limit=1))"
```

### Step 3 — Pre-download ML models (optional but recommended)

Models are large and slow to download on first run. Pre-download to the named volume:

```bash
docker compose run --rm embeddings-clap python -c "
import laion_clap
m = laion_clap.CLAP_Module(enable_fusion=False)
m.load_ckpt()
print('CLAP ready')
"

docker compose run --rm embeddings-mert python -c "
from transformers import AutoModel, Wav2Vec2FeatureExtractor
AutoModel.from_pretrained('m-a-p/MERT-v1-95M', trust_remote_code=True, cache_dir='/models/mert')
print('MERT ready')
"
```

### Step 4 — Start everything

```bash
docker compose up --build
```

Services start in order: `db` → `migrate` (runs and exits) → all workers and api-gateway.

### Step 5 — Verify

```bash
# API health
curl http://localhost:8000/health

# Trigger sync
curl -X POST http://localhost:8000/v1/account/sync

# Check sync status
curl http://localhost:8000/v1/account/sync/status

# Watch logs
docker compose logs -f ytm-sync
docker compose logs -f ytdlp-downloader
docker compose logs -f embeddings-clap

# Inspect DB state
docker compose exec db psql -U ytune -d ytune -c \
  "SELECT status, COUNT(*) FROM track_downloaded GROUP BY status;"
```

### Step 6 — Re-run migrations after schema change

```bash
make migrate
# or:
docker compose run --rm migrate
```

### Step 7 — Run tests

```bash
# Unit tests (no DB needed)
docker compose run --rm api-gateway python -m pytest services/api_gateway/tests/unit/ -v

# Integration tests (requires DB)
docker compose run --rm api-gateway python -m pytest services/api_gateway/tests/integration/ -v
```

## Makefile

```makefile
.PHONY: up down build migrate logs-sync logs-dl shell-db extract-auth test sync

up:
	docker compose up --build

down:
	docker compose down -v

build:
	docker compose build

migrate:
	docker compose run --rm migrate

logs-sync:
	docker compose logs -f ytm-sync

logs-dl:
	docker compose logs -f ytdlp-downloader

logs-clap:
	docker compose logs -f embeddings-clap

logs-mert:
	docker compose logs -f embeddings-mert

shell-db:
	docker compose exec db psql -U ytune -d ytune

extract-auth:
	python scripts/extract_auth.py --browser chrome --output secrets/ytm_auth.json

test:
	docker compose run --rm api-gateway python -m pytest services/ -v

sync:
	curl -s -X POST http://localhost:8000/v1/account/sync | python -m json.tool

status:
	curl -s http://localhost:8000/v1/account/sync/status | python -m json.tool
```

## `db/init/01_enable_extensions.sql`

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()
-- CREATE EXTENSION IF NOT EXISTS vector; -- uncomment when migrating to pgvector
```
