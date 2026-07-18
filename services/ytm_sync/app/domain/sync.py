"""
Core sync orchestration.

Flow:
  1. Claim one queued sync_job (SKIP LOCKED — safe against duplicate workers).
  2. Fetch user playlists from YTM.
  3. For each user playlist (parallel, semaphore=3):
       - Fetch tracks + related playlists.
       - Upsert tracks and suggested playlists.
       - Collect related playlist IDs.
  4. For each unique suggested playlist (parallel, semaphore=3):
       - Fetch tracks (no related expansion).
       - Upsert tracks.
  5. Per track: ensure pending rows + notify 4 worker queues.
  6. Write stats to sync_job and mark done (or error).

Design notes:
  - Each parallel coroutine acquires its own pool connection to avoid
    sharing a single asyncpg Connection across concurrent tasks.
  - suggested_ids collection: coroutines RETURN their lists; caller flattens.
    No shared mutable state, no lock needed.
  - ytmusicapi calls are sync (blocking IO). Run via run_in_executor so
    the event loop stays responsive.
"""
from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import structlog

from core.db.track_downloaded import notify as notify_download_queue
from core.utils.retry import ytm_retry
from services.ytm_sync.app.infra.db.repositories import (
    PlaylistRepository,
    SyncJobRepository,
    TrackRepository,
)
from services.ytm_sync.app.infra.ytm.client import YTMClient

log = structlog.get_logger()

_PARALLEL_LIMIT = 3  # concurrent YTM requests — stay gentle on rate limits
_SUGGESTED_CAP = 50  # max suggested playlists expanded per sync


@dataclass
class SyncStats:
    user_playlists: int = 0
    suggested_playlists: int = 0
    tracks_synced: int = 0
    tracks_skipped: int = 0
    playlist_errors: int = 0

    def to_dict(self) -> dict:
        return {
            "user_playlists": self.user_playlists,
            "suggested_playlists": self.suggested_playlists,
            "tracks_synced": self.tracks_synced,
            "tracks_skipped": self.tracks_skipped,
            "playlist_errors": self.playlist_errors,
        }


async def run_sync(pool: asyncpg.Pool, auth_file: str) -> None:
    """
    Entry point called by the main listener loop.
    Acquires its own connections from pool — does NOT accept a pre-acquired conn.
    """
    async with pool.acquire() as conn:
        job_repo = SyncJobRepository(conn)
        job_id = await job_repo.claim_queued()

    if not job_id:
        log.info("no_queued_sync_job")
        return

    structlog.contextvars.bind_contextvars(sync_id=job_id)
    log.info("sync_started")
    stats = SyncStats()

    try:
        ytm = _make_ytm_client(auth_file)
        await _do_sync(pool, ytm, stats)

        async with pool.acquire() as conn:
            await SyncJobRepository(conn).finish(job_id, stats.to_dict())
        log.info("sync_done", **stats.to_dict())

    except Exception as exc:
        log.error("sync_failed", error=str(exc), traceback=traceback.format_exc())
        async with pool.acquire() as conn:
            await SyncJobRepository(conn).fail(job_id, str(exc), stats.to_dict())


def _make_ytm_client(auth_file: str) -> YTMClient:
    return YTMClient(auth_file)


async def _do_sync(pool: asyncpg.Pool, ytm: YTMClient, stats: SyncStats) -> None:
    sem = asyncio.Semaphore(_PARALLEL_LIMIT)

    # ── 1. Fetch user playlists ──────────────────────────────────────────────
    user_playlists = await _fetch_library_playlists(ytm)
    log.info("library_playlists_fetched", count=len(user_playlists))

    async with pool.acquire() as conn:
        pl_repo = PlaylistRepository(conn)
        await pl_repo.upsert_many(user_playlists, "user")
    stats.user_playlists = len(user_playlists)

    # ── 2. Process user playlists in parallel ────────────────────────────────
    async def handle_user_playlist(pl: dict) -> list[dict]:
        """Returns list of related playlist dicts."""
        async with sem:
            playlist_id = pl["id"]
            structlog.contextvars.bind_contextvars(playlist_id=playlist_id)
            try:
                tracks, related = await _fetch_playlist(ytm, playlist_id, related=True)
                async with pool.acquire() as conn:
                    tr = TrackRepository(conn)
                    pr = PlaylistRepository(conn)
                    count = await tr.upsert_many(tracks, "user", playlist_id)
                    await pr.update_song_count(playlist_id, count)
                    await pr.upsert_many(related, "suggested")
                    for t in tracks:
                        await tr.ensure_processing_rows(t["video_id"])
                        await notify_download_queue(conn, t["video_id"], event="track_added")
                stats.tracks_synced += count
                log.info("user_playlist_done", tracks=count, related=len(related))
                return related
            except Exception as exc:
                log.error("user_playlist_error", error=str(exc))
                stats.playlist_errors += 1
                return []

    results = await asyncio.gather(
        *[handle_user_playlist(pl) for pl in user_playlists],
        return_exceptions=False,
    )

    # Collect unique suggested playlist IDs, cap at 50
    seen: set[str] = set()
    suggested: list[dict] = []
    for rel_list in results:
        for pl in rel_list:
            if pl["id"] and pl["id"] not in seen:
                seen.add(pl["id"])
                suggested.append(pl)
                if len(suggested) >= _SUGGESTED_CAP:
                    break
        if len(suggested) >= _SUGGESTED_CAP:
            break

    log.info("suggested_playlists_collected", count=len(suggested))
    stats.suggested_playlists = len(suggested)

    # ── 3. Process suggested playlists in parallel ───────────────────────────
    async def handle_suggested_playlist(pl: dict) -> None:
        async with sem:
            playlist_id = pl["id"]
            structlog.contextvars.bind_contextvars(playlist_id=playlist_id)
            try:
                tracks, _ = await _fetch_playlist(ytm, playlist_id, related=False)
                async with pool.acquire() as conn:
                    tr = TrackRepository(conn)
                    pr = PlaylistRepository(conn)
                    await pr.upsert(pl, "suggested")
                    count = await tr.upsert_many(tracks, "suggested", playlist_id)
                    await pr.update_song_count(playlist_id, count)
                    for t in tracks:
                        await tr.ensure_processing_rows(t["video_id"])
                        await notify_download_queue(conn, t["video_id"], event="track_added")
                stats.tracks_synced += count
                log.info("suggested_playlist_done", tracks=count)
            except Exception as exc:
                log.error("suggested_playlist_error", error=str(exc))
                stats.playlist_errors += 1

    await asyncio.gather(
        *[handle_suggested_playlist(pl) for pl in suggested],
        return_exceptions=False,
    )


# ── YTM fetch helpers (retry-decorated, offloaded to thread) ──────────────────

@ytm_retry
async def _fetch_library_playlists(ytm: YTMClient) -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, ytm.get_library_playlists)


@ytm_retry
async def _fetch_playlist(
    ytm: YTMClient, playlist_id: str, *, related: bool
) -> tuple[list[dict], list[dict]]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: ytm.get_playlist(playlist_id, related=related)
    )
