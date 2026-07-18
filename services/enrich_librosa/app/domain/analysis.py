"""
Core librosa analysis logic.

Flow:
  1. Claim a batch of pending local_librosa rows that have a file_path
     (FOR UPDATE SKIP LOCKED — rows without a file yet are left alone, they get
     re-notified once ytdlp-downloader finishes them).
  2. Extract features via librosa, bounded by a semaphore (settings.max_concurrent_workers).
  3. On success: mark done with the extracted feature dict.
  4. On failure: retry up to 3 times, then mark error.
  5. Repeat until a claim comes back empty — drains the whole backlog per wakeup.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor

import asyncpg
import structlog

from core.utils.retry import embed_retry
from services.enrich_librosa.app.infra.db.repositories import LibrosaRepository

log = structlog.get_logger()

_BATCH_SIZE = 5

_executor: ProcessPoolExecutor | None = None


def _get_executor(max_workers: int) -> ProcessPoolExecutor:
    """
    librosa/numpy is CPU-bound — run each analysis in its own process so it
    never blocks the event loop and concurrent analyses can't share broken state.
    """
    global _executor
    if _executor is None:
        _executor = ProcessPoolExecutor(max_workers=max_workers)
    return _executor


async def process_pending_librosa(pool: asyncpg.Pool, settings) -> None:
    semaphore = asyncio.Semaphore(int(settings.max_concurrent_workers))
    executor = _get_executor(int(settings.max_concurrent_workers))

    while True:
        async with pool.acquire() as conn:
            rows = await LibrosaRepository(conn).claim_pending(limit=_BATCH_SIZE)
        if not rows:
            return
        await asyncio.gather(*[_handle_one(pool, semaphore, executor, settings, r) for r in rows])


async def _handle_one(
    pool: asyncpg.Pool,
    semaphore: asyncio.Semaphore,
    executor: ProcessPoolExecutor,
    settings,
    row: asyncpg.Record,
) -> None:
    async with semaphore:
        video_id = row["video_id"]
        structlog.contextvars.bind_contextvars(video_id=video_id)
        try:
            features = await extract_features(executor, row["file_path"], settings.max_analysis_seconds)
            async with pool.acquire() as conn:
                await LibrosaRepository(conn).mark_done(video_id, features)
            log.info("librosa_done")
        except Exception as exc:
            log.error("librosa_failed", error=str(exc))
            async with pool.acquire() as conn:
                await LibrosaRepository(conn).mark_failed_or_retry(video_id, str(exc))


@embed_retry
async def extract_features(executor: ProcessPoolExecutor, file_path: str, max_seconds: int) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _extract_features_sync, file_path, max_seconds)


def _extract_features_sync(file_path: str, max_seconds: int) -> dict:
    """Runs in a worker process — must be a top-level function (picklable), no closures."""
    import librosa
    import numpy as np

    y, sr = librosa.load(file_path, sr=None, mono=True, duration=max_seconds)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    key = int(np.argmax(np.mean(chroma, axis=1)))
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    return {
        # librosa's beat_track returns tempo as a 1-element array, not a scalar;
        # np.ravel(...)[0] handles that and a plain scalar the same way.
        "tempo": float(np.ravel(tempo)[0]),
        "duration": float(librosa.get_duration(y=y, sr=sr)),
        "key": key,
        "spectral_centroid_mean": float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr))),
        "spectral_rolloff_mean": float(np.mean(librosa.feature.spectral_rolloff(y=y, sr=sr))),
        "zero_crossing_rate_mean": float(np.mean(librosa.feature.zero_crossing_rate(y))),
        "rms_mean": float(np.mean(librosa.feature.rms(y=y))),
        "mfcc_mean": np.mean(mfcc, axis=1).tolist(),
    }
