"""
MERT audio embedding logic.

Flow:
  1. Claim a batch of pending local_mert rows that have a file_path.
  2. Embed each via MERT-v1-95M, mean-pooling the last hidden state to 768-dim.
  3. On success: mark done with the embedding vector.
  4. On failure: retry up to 3 times, then mark error.
  5. Repeat until the backlog is drained.
"""
from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import asyncpg
import structlog

from core.utils.retry import embed_retry
from services.embeddings_mert.app.infra.db.repositories import MertRepository

log = structlog.get_logger()

_BATCH_SIZE = 5
_MODEL_ID = "m-a-p/MERT-v1-95M"
_SAMPLE_RATE = 24000  # MERT's expected sample rate
_MAX_SECONDS = 60     # cap audio before feeding to model — full songs OOM on CPU

_executor: ProcessPoolExecutor | None = None
_model = None
_processor = None


def _init_worker(device: str) -> None:
    """Loads MERT model + processor once per worker process."""
    global _model, _processor
    import sys, traceback
    try:
        import torch
        from transformers import AutoModel, AutoProcessor

        _processor = AutoProcessor.from_pretrained(_MODEL_ID, trust_remote_code=True)
        _model = AutoModel.from_pretrained(_MODEL_ID, trust_remote_code=True)
        _model.eval()
        if device != "cpu":
            _model = _model.to(device)
        print("mert worker init done", flush=True)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        raise


def _get_executor(max_workers: int, device: str) -> ProcessPoolExecutor:
    global _executor
    if _executor is None:
        # PyTorch + transformers don't survive fork (inherited thread/CUDA state);
        # spawn starts a fresh interpreter so the initializer runs in a clean process.
        _executor = ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_init_worker,
            initargs=(device,),
        )
    return _executor


async def process_pending_mert(pool: asyncpg.Pool, settings) -> None:
    semaphore = asyncio.Semaphore(int(settings.max_concurrent_workers))
    executor = _get_executor(int(settings.max_concurrent_workers), settings.device)
    max_seconds = int(settings.max_analysis_seconds)

    while True:
        async with pool.acquire() as conn:
            rows = await MertRepository(conn).claim_pending(limit=_BATCH_SIZE)
        if not rows:
            return
        await asyncio.gather(*[_handle_one(pool, semaphore, executor, r, max_seconds) for r in rows])


async def _handle_one(
    pool: asyncpg.Pool, semaphore: asyncio.Semaphore, executor: ProcessPoolExecutor,
    row: asyncpg.Record, max_seconds: int,
) -> None:
    async with semaphore:
        video_id = row["video_id"]
        structlog.contextvars.bind_contextvars(video_id=video_id)
        try:
            embedding = await embed_file(executor, row["file_path"], max_seconds)
            async with pool.acquire() as conn:
                await MertRepository(conn).mark_done(video_id, embedding)
            log.info("mert_done")
        except Exception as exc:
            log.error("mert_failed", error=str(exc))
            async with pool.acquire() as conn:
                await MertRepository(conn).mark_failed_or_retry(video_id, str(exc))


@embed_retry
async def embed_file(executor: ProcessPoolExecutor, file_path: str, max_seconds: int) -> list[float]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _embed_file_sync, file_path, max_seconds)


def _embed_file_sync(file_path: str, max_seconds: int) -> list[float]:
    """Runs in a worker process. Loads audio, runs MERT, mean-pools last hidden state."""
    import librosa
    import torch

    audio, _ = librosa.load(file_path, sr=_SAMPLE_RATE, mono=True, duration=max_seconds)
    inputs = _processor(audio, sampling_rate=_SAMPLE_RATE, return_tensors="pt")
    with torch.no_grad():
        outputs = _model(**inputs, output_hidden_states=True)
    # last_hidden_state: [1, time_steps, 768] -> mean over time -> [768]
    embedding = outputs.last_hidden_state.mean(dim=1).squeeze().numpy()
    return embedding.tolist()
