# Shared Core Library

`core/` is not a service. It is a shared Python package mounted into every service container via
`COPY core/ ./core/` in each Dockerfile. Changes here affect all services.

## Directory Layout

```
core/
├── __init__.py
├── config.py           # pydantic-settings BaseSettings subclasses
├── logging.py          # structlog JSON setup
└── utils/
    ├── __init__.py
    └── retry.py        # tenacity retry configs
```

## `core/config.py`

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str
    storage_root: str = "/storage"
    log_level: str = "INFO"
    db_max_pool_size: int = 10

class YTMSyncSettings(Settings):
    ytm_auth_file: str

class EmbeddingSettings(Settings):
    clap_model_path: str = "/models/clap"
    mert_model_path: str = "/models/mert"
    device: str = "cpu"
    max_concurrent_workers: int = 2

class SuggestionSettings(Settings):
    cluster_k: int = 8
    cluster_trigger_threshold: int = 10
```

Each service imports its own settings class:
```python
from core.config import YTMSyncSettings
settings = YTMSyncSettings()
```

## `core/logging.py`

```python
import structlog, logging, sys

def configure_logging(service_name: str, level: str = "INFO"):
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_logger_name,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
    )
    structlog.contextvars.bind_contextvars(service=service_name)
```

**Usage with per-request context**:
```python
import structlog
structlog.contextvars.bind_contextvars(sync_id=str(sync_id), video_id=video_id)
log = structlog.get_logger()
log.info("processing_track")
# → {"service":"ytm-sync","sync_id":"...","video_id":"...","event":"processing_track","timestamp":"..."}
```

## `core/utils/retry.py`

```python
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type, before_sleep_log
)
import httpx, logging

log = logging.getLogger(__name__)

ytm_retry = retry(
    retry=retry_if_exception_type((httpx.TransientError, httpx.TimeoutException, Exception)),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    before_sleep=before_sleep_log(log, logging.WARNING),
)

download_retry = retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=5, max=60),
)

embed_retry = retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=3, max=15),
)
```

**Usage**:
```python
from core.utils.retry import ytm_retry

@ytm_retry
async def fetch_playlist(ytm_client, playlist_id: str) -> dict:
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: ytm_client.get_playlist(playlist_id, limit=None)
    )
```

## `core/__init__.py`

Empty. Required for `PYTHONPATH=/app` module resolution.

## How Services Import from Core

Because `PYTHONPATH=/app` and `core/` is at `/app/core/`:

```python
from core.config import Settings
from core.logging import configure_logging
from core.utils.retry import ytm_retry
```

No `pip install -e .` or `pyproject.toml` needed. Simple path-based import.
