import base64
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
import asyncpg

from services.api_gateway.app.api.deps import get_db_conn
from services.api_gateway.app.infra.db.repositories import PlaylistRepository

router = APIRouter()

VALID_PLAYLIST_TYPES = {"user", "suggested"}


def _decode_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        return json.loads(base64.b64decode(cursor).decode())["after_id"]
    except Exception:
        return None


def _encode_cursor(last_id: str) -> str:
    return base64.b64encode(json.dumps({"after_id": last_id}).encode()).decode()


@router.get("")
async def list_playlists(
    type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
    conn: asyncpg.Connection = Depends(get_db_conn),
):
    if type and type not in VALID_PLAYLIST_TYPES:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "VALIDATION_ERROR", "message": f"type must be one of {VALID_PLAYLIST_TYPES}"}},
        )

    after_id = _decode_cursor(cursor)
    repo = PlaylistRepository(conn)
    rows = await repo.list(playlist_type=type, limit=limit + 1, after_id=after_id)

    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = _encode_cursor(items[-1]["id"]) if has_more and items else None

    return {
        "items": [
            {
                "id": r["id"],
                "title": r["title"],
                "owner": r["owner"],
                "playlist_type": r["playlist_type"],
                "song_count": r["song_count"],
                "sync_state": r["sync_state"],
                "last_synced_at": r["last_synced_at"].isoformat() if r.get("last_synced_at") else None,
            }
            for r in items
        ],
        "next_cursor": next_cursor,
    }


@router.get("/{playlist_id}")
async def get_playlist(
    playlist_id: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
    conn: asyncpg.Connection = Depends(get_db_conn),
):
    repo = PlaylistRepository(conn)
    playlist = await repo.get(playlist_id)
    if not playlist:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Playlist not found"}},
        )

    after_id = _decode_cursor(cursor)
    tracks = await repo.get_tracks(playlist_id, limit=limit + 1, after_id=after_id)

    has_more = len(tracks) > limit
    track_page = tracks[:limit]
    next_cursor = _encode_cursor(track_page[-1]["id"]) if has_more and track_page else None

    return {
        "id": playlist["id"],
        "title": playlist["title"],
        "owner": playlist["owner"],
        "playlist_type": playlist["playlist_type"],
        "song_count": playlist["song_count"],
        "sync_state": playlist["sync_state"],
        "last_synced_at": playlist["last_synced_at"].isoformat() if playlist.get("last_synced_at") else None,
        "tracks": [
            {
                "id": t["id"],
                "title": t["title"],
                "artist": t["artist"],
                "album": t.get("album"),
                "duration_seconds": t["duration_seconds"],
                "track_type": t["track_type"],
                "download_status": t["download_status"],
                "librosa_status": t["librosa_status"],
                "clap_status": t["clap_status"],
                "mert_status": t["mert_status"],
            }
            for t in track_page
        ],
        "next_cursor": next_cursor,
    }
