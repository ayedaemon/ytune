from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
import asyncpg

from services.api_gateway.app.api.deps import get_db_conn
from services.api_gateway.app.domain.mert import bulk_trigger_embed, get_queue_status

router = APIRouter()


class TriggerRequest(BaseModel):
    ids: list[str] | None = None  # omit or null = all tracks
    force: bool = False


@router.get("")
async def mert_queue_status(
    errors_limit: Annotated[int, Query(ge=1, le=200)] = 20,
    conn: asyncpg.Connection = Depends(get_db_conn),
):
    return await get_queue_status(conn, errors_limit=errors_limit)


@router.post("")
async def trigger_embeddings(
    body: TriggerRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
):
    return await bulk_trigger_embed(conn, body.ids, body.force)
