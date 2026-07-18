from fastapi import APIRouter, Depends, HTTPException
import asyncpg

from services.api_gateway.app.api.deps import get_db_conn
from services.api_gateway.app.domain.sync import (
    create_sync_job,
    get_latest_sync_status,
    is_sync_running,
)

router = APIRouter()


@router.post("/sync", status_code=202)
async def trigger_sync(conn: asyncpg.Connection = Depends(get_db_conn)):
    if await is_sync_running(conn):
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": "SYNC_ALREADY_RUNNING", "message": "A sync is already in progress"}},
        )
    sync_id = await create_sync_job(conn)
    return {"sync_id": sync_id, "status": "queued"}


@router.get("/sync/status")
async def sync_status(conn: asyncpg.Connection = Depends(get_db_conn)):
    return await get_latest_sync_status(conn)
