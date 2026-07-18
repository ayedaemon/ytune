"""
Sync orchestration domain logic. No SQL — delegates to SyncJobRepository.
"""
from __future__ import annotations

import json

import asyncpg

from services.api_gateway.app.infra.db.repositories import SyncJobRepository


async def create_sync_job(conn: asyncpg.Connection) -> str:
    repo = SyncJobRepository(conn)
    job_id = await repo.create()
    payload = json.dumps({"sync_id": job_id})
    await conn.execute("SELECT pg_notify('sync_jobs_queue', $1)", payload)
    return job_id


async def get_latest_sync_status(conn: asyncpg.Connection) -> dict:
    repo = SyncJobRepository(conn)
    job = await repo.get_latest()
    if not job:
        return {"status": "never_run"}
    return {
        "sync_id": str(job["id"]),
        "status": job["status"],
        "started_at": job["started_at"].isoformat() if job["started_at"] else None,
        "finished_at": job["finished_at"].isoformat() if job["finished_at"] else None,
        "error": job.get("error"),
        "stats": (json.loads(job["stats"]) if isinstance(job.get("stats"), str) else job.get("stats")) or {},
    }


async def is_sync_running(conn: asyncpg.Connection) -> bool:
    repo = SyncJobRepository(conn)
    return (await repo.get_running()) is not None
