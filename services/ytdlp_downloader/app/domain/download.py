"""
Core download logic.

Flow:
  1. Claim a batch of pending track_downloaded rows (FOR UPDATE SKIP LOCKED).
  2. Download each via yt-dlp, bounded by a semaphore (settings.max_concurrent_downloads).
  3. On success: mark done, copy file_path into local_librosa/clap/mert (they were
     seeded NULL by ytm-sync), notify their queues.
  4. On failure: retry up to 3 times, then mark error.
  5. Repeat until a claim comes back empty — drains the whole backlog per wakeup.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from concurrent.futures import ProcessPoolExecutor

import asyncpg
import structlog

from core.utils.retry import download_retry
from services.ytdlp_downloader.app.infra.db.repositories import DownloadRepository
from services.ytdlp_downloader.app.infra.storage.fs import get_output_template

log = structlog.get_logger()

_BATCH_SIZE = 10

_executor: ProcessPoolExecutor | None = None


def _get_executor(max_workers: int) -> ProcessPoolExecutor:
    """
    yt-dlp/ffmpeg isn't safe to run as concurrent YoutubeDL() instances inside one
    process — under load, one instance's postprocessor occasionally fails to delete
    its pre-conversion source file (module-level caches racing across threads).
    A process pool gives each concurrent download a fully isolated interpreter.
    """
    global _executor
    if _executor is None:
        _executor = ProcessPoolExecutor(max_workers=max_workers)
    return _executor


async def process_pending_downloads(pool: asyncpg.Pool, settings) -> None:
    semaphore = asyncio.Semaphore(int(settings.max_concurrent_downloads))
    executor = _get_executor(int(settings.max_concurrent_downloads))

    while True:
        async with pool.acquire() as conn:
            rows = await DownloadRepository(conn).claim_pending(limit=_BATCH_SIZE)
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
            file_path = await download_track(
                executor,
                video_id,
                settings.storage_root,
                settings.audio_format,
                settings.download_outtmpl,
                settings.ytm_cookies_file,
            )
            async with pool.acquire() as conn:
                repo = DownloadRepository(conn)
                await repo.mark_done(video_id, file_path)
                await repo.update_enrichment_paths(video_id, file_path)
                await repo.notify_enrichment_queues(video_id)
            log.info("download_done", file_path=file_path)
        except Exception as exc:
            log.error("download_failed", error=str(exc))
            async with pool.acquire() as conn:
                await DownloadRepository(conn).mark_failed_or_retry(video_id, str(exc))


@download_retry
async def download_track(
    executor: ProcessPoolExecutor,
    video_id: str,
    storage_root: str,
    audio_format: str,
    outtmpl: str,
    cookies_file: str,
) -> str:
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": get_output_template(storage_root, outtmpl),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": audio_format}],
        "quiet": True,
        "no_warnings": True,
        # Authenticated (cookie) requests get SABR-only formats unless yt-dlp can
        # solve YouTube's signature/n-challenges — that needs the EJS solver script,
        # which it otherwise skips downloading (cached-only by default).
        "remote_components": {"ejs:github"},
        # Some tracks have absurdly long %(artist)s credit lists (compilation/various-
        # artist metadata) that blow past a filesystem's ~255-byte path component limit
        # and crash mkdir with ENAMETOOLONG. Caps the whole rendered path, same safety
        # net the old custom templating used to provide before we switched to yt-dlp's
        # native output template.
        "trim_file_name": 200,
    }
    scratch_cookies = None
    if os.path.isfile(cookies_file):
        # cookies_file lives on a read-only mount and yt-dlp rewrites its cookiejar on
        # close(); give each call its own writable copy so concurrent processes never
        # touch the same file (or the real credentials).
        fd, scratch_cookies = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        shutil.copy(cookies_file, scratch_cookies)
        ydl_opts["cookiefile"] = scratch_cookies

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(executor, _run_download, video_id, ydl_opts)
    finally:
        if scratch_cookies:
            os.remove(scratch_cookies)


def _run_download(video_id: str, ydl_opts: dict) -> str:
    """
    Runs in a worker process — must be a top-level function (picklable), no closures.
    Returns the actual on-disk path yt-dlp resolved the template to, after
    postprocessing (e.g. the .mp3 FFmpegExtractAudio produced, not the raw download).
    """
    import yt_dlp

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
        # requested_downloads is None when yt-dlp skips (file already exists) or the
        # postprocessor replaces the entry. Walk several fallbacks:
        downloads = info.get("requested_downloads") or []
        if downloads and downloads[0].get("filepath"):
            return downloads[0]["filepath"]
        # After FFmpegExtractAudio, info["ext"] is updated to the target codec so
        # prepare_filename gives the correct final path.
        return ydl.prepare_filename(info)
