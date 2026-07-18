"""
Core CLAP embedding logic.

Flow:
  1. Claim a batch of pending local_clap rows that have a file_path (FOR UPDATE
     SKIP LOCKED — rows without a file yet are left alone, they get re-notified
     once ytdlp-downloader finishes them).
  2. Embed each via the CLAP model, bounded by a semaphore (settings.max_concurrent_workers).
  3. On success: mark done with the 512-dim embedding vector.
  4. On failure: retry up to 3 times, then mark error.
  5. Repeat until a claim comes back empty — drains the whole backlog per wakeup.
"""
from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import asyncpg
import structlog

from core.utils.retry import embed_retry
from services.embeddings_clap.app.infra.db.repositories import ClapRepository

log = structlog.get_logger()

_BATCH_SIZE = 5

_executor: ProcessPoolExecutor | None = None
_model = None  # only ever set inside a worker process, by _init_worker


def _init_worker(device: str) -> None:
    """Runs once per worker process — loads the model exactly once from the baked checkpoint."""
    global _model
    import laion_clap

    _model = laion_clap.CLAP_Module(enable_fusion=False, device=device)
    _model.load_ckpt()  # finds baked 630k-audioset-best.pt in package dir, no download


def _get_executor(max_workers: int, device: str) -> ProcessPoolExecutor:
    """
    Dedicated worker process(es) rather than a thread pool: isolates CLAP/torch so a
    crash there can't take the whole service down. Initializer loads the model once per
    worker.
    """
    global _executor
    if _executor is None:
        _executor = ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_init_worker,
            initargs=(device,),
        )
    return _executor


async def process_pending_clap(pool: asyncpg.Pool, settings) -> None:
    semaphore = asyncio.Semaphore(int(settings.max_concurrent_workers))
    executor = _get_executor(int(settings.max_concurrent_workers), settings.device)

    while True:
        async with pool.acquire() as conn:
            rows = await ClapRepository(conn).claim_pending(limit=_BATCH_SIZE)
        if not rows:
            return
        await asyncio.gather(*[_handle_one(pool, semaphore, executor, r) for r in rows])


async def _handle_one(
    pool: asyncpg.Pool, semaphore: asyncio.Semaphore, executor: ProcessPoolExecutor, row: asyncpg.Record
) -> None:
    async with semaphore:
        video_id = row["video_id"]
        structlog.contextvars.bind_contextvars(video_id=video_id)
        try:
            embedding = await embed_file(executor, row["file_path"])
            async with pool.acquire() as conn:
                await ClapRepository(conn).mark_done(video_id, embedding)
            log.info("clap_done")
        except Exception as exc:
            log.error("clap_failed", error=str(exc))
            async with pool.acquire() as conn:
                await ClapRepository(conn).mark_failed_or_retry(video_id, str(exc))


@embed_retry
async def embed_file(executor: ProcessPoolExecutor, file_path: str) -> list[float]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _embed_file_sync, file_path)


def _embed_file_sync(file_path: str) -> list[float]:
    """Runs in a worker process — _model was loaded once there by _init_worker."""
    embedding = _model.get_audio_embedding_from_filelist([file_path], use_tensor=False)
    return embedding[0].tolist()
