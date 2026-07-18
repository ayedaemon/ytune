import base64
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
import asyncpg

from services.api_gateway.app.api.deps import get_db_conn
from services.api_gateway.app.domain.clap import get_track_clap
from services.api_gateway.app.domain.mert import get_track_mert
from services.api_gateway.app.domain.downloads import get_track_download
from services.api_gateway.app.domain.librosa import get_track_librosa
from services.api_gateway.app.infra.db.repositories import TrackRepository

router = APIRouter()


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
async def list_tracks(
    type: Annotated[str | None, Query()] = None,
    has_download: Annotated[bool | None, Query()] = None,
    has_librosa: Annotated[bool | None, Query()] = None,
    has_clap: Annotated[bool | None, Query()] = None,
    has_mert: Annotated[bool | None, Query()] = None,
    has_errors: Annotated[bool | None, Query()] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
    conn: asyncpg.Connection = Depends(get_db_conn),
):
    if type and type not in {"user", "suggested"}:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "VALIDATION_ERROR", "message": "type must be 'user' or 'suggested'"}},
        )

    after_id = _decode_cursor(cursor)
    repo = TrackRepository(conn)
    rows = await repo.list(
        track_type=type,
        has_download=has_download,
        has_librosa=has_librosa,
        has_clap=has_clap,
        has_mert=has_mert,
        has_errors=has_errors,
        q=q,
        limit=limit + 1,
        after_id=after_id,
    )

    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = _encode_cursor(items[-1]["id"]) if has_more and items else None

    return {
        "items": [
            {
                "id": t["id"],
                "title": t["title"],
                "artist": t["artist"],
                "duration_seconds": t["duration_seconds"],
                "track_type": t["track_type"],
                "download_status": t["download_status"],
                "librosa_status": t["librosa_status"],
                "clap_status": t["clap_status"],
                "mert_status": t["mert_status"],
            }
            for t in items
        ],
        "next_cursor": next_cursor,
    }


@router.get("/{video_id}")
async def get_track(
    video_id: str,
    include_features: Annotated[bool, Query()] = False,
    conn: asyncpg.Connection = Depends(get_db_conn),
):
    repo = TrackRepository(conn)
    row = await repo.get(video_id, include_features=include_features)
    if not row:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Track not found"}},
        )

    librosa_features = None
    if row.get("librosa_features") and include_features:
        raw = row["librosa_features"]
        librosa_features = json.loads(raw) if isinstance(raw, (str, bytes)) else raw

    clap_embedding = None
    mert_embedding = None
    if include_features:
        raw_clap = row.get("clap_embedding")
        raw_mert = row.get("mert_embedding")
        if raw_clap:
            clap_embedding = json.loads(raw_clap) if isinstance(raw_clap, (str, bytes)) else raw_clap
        if raw_mert:
            mert_embedding = json.loads(raw_mert) if isinstance(raw_mert, (str, bytes)) else raw_mert

    return {
        "id": row["id"],
        "title": row["title"],
        "artist": row["artist"],
        "album": row.get("album"),
        "duration_seconds": row["duration_seconds"],
        "track_type": row["track_type"],
        "source_playlist_id": row["source_playlist_id"],
        "download": {
            "status": row["download_status"],
            "file_path": row.get("download_file_path"),
        },
        "librosa": {
            "status": row["librosa_status"],
            "features": librosa_features,
        },
        "clap": {
            "status": row["clap_status"],
            "embedding": clap_embedding,
        },
        "mert": {
            "status": row["mert_status"],
            "embedding": mert_embedding,
        },
    }


@router.get("/{video_id}/download")
async def get_track_download_route(
    video_id: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
):
    result = await get_track_download(conn, video_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Track not found"}},
        )
    return result


@router.get("/{video_id}/librosa")
async def get_track_librosa_route(
    video_id: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
):
    result = await get_track_librosa(conn, video_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Track not found"}},
        )
    return result


@router.get("/{video_id}/clap")
async def get_track_clap_route(
    video_id: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
):
    result = await get_track_clap(conn, video_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Track not found"}},
        )
    return result


@router.get("/{video_id}/mert")
async def get_track_mert_route(
    video_id: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
):
    result = await get_track_mert(conn, video_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Track not found"}},
        )
    return result
