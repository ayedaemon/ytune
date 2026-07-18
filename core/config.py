from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.resources import suggest_concurrency


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Lets docker-compose pass an empty-string env var (its `${VAR:-}` fallback
        # when unset) through as "unset" instead of failing int/bool validation —
        # needed so the default_factory-computed fields below still kick in.
        env_ignore_empty=True,
    )

    database_url: str = "postgresql://ytmusic:password@localhost:5432/ytmusic_db"
    storage_root: str = "/storage"
    log_level: str = "INFO"
    db_max_pool_size: int = 10
    db_min_pool_size: int = 2


class YTMSyncSettings(Settings):
    ytm_auth_file: str = "/app/auth/ytmusic.json"


class EmbeddingSettings(Settings):
    device: str = "cpu"
    # The model isn't safe for concurrent inference from multiple workers on CPU —
    # keep at 1 unless running on a GPU with room for more.
    max_concurrent_workers: int = 1
    max_analysis_seconds: int = 60  # clip audio before inference to prevent OOM


SUPPORTED_AUDIO_FORMATS = {"opus", "mp3", "m4a", "aac", "flac", "wav", "vorbis"}

class DownloaderSettings(Settings):
    ytm_cookies_file: str = "/app/auth/cookies.txt"
    # Sized to the container's actual memory/CPU limits (see core/resources.py) rather
    # than a flat number — a yt-dlp+ffmpeg download+transcode runs ~180MB peak RSS.
    max_concurrent_downloads: int = Field(default_factory=lambda: suggest_concurrency(mem_per_worker_mb=180))
    audio_format: str = "mp3"
    # yt-dlp output template (https://github.com/yt-dlp/yt-dlp#output-template),
    # relative to storage_root. yt-dlp does the templating, sanitization, and
    # subdirectory creation itself — any %(field)s it extracts is available
    # (%(title)s, %(artist)s, %(album)s, %(upload_date)s, ...). Default keeps one
    # flat file per video_id, always unique; a template that drops %(id)s can
    # collide across tracks that render to the same name, same as plain yt-dlp.
    download_outtmpl: str = "%(id)s.%(ext)s"


class LibrosaSettings(Settings):
    # librosa/numpy analysis runs ~400MB peak RSS per track (audio buffer + MFCC/chroma
    # arrays + numba JIT overhead) — see core/resources.py for the sizing logic.
    max_concurrent_workers: int = Field(default_factory=lambda: suggest_concurrency(mem_per_worker_mb=400))
    max_analysis_seconds: int = 300  # clip audio at this length to avoid OOM on very long files


class SuggestionSettings(Settings):
    cluster_k: int = 8
    cluster_trigger_threshold: int = 10
