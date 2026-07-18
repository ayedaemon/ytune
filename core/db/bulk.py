"""
Generic bulk status-reset + notify, shared across the enrichment tables that
follow the same pending -> in-progress -> done|error state machine
(track_downloaded, local_librosa, and eventually local_clap/local_mert).
`table`/`in_progress_status`/`notify_channel` are always literals passed by
each table's own thin wrapper (core.db.track_downloaded etc.) — never user input.
"""
from __future__ import annotations

import json

import asyncpg


async def reset_many(
    conn: asyncpg.Connection,
    table: str,
    in_progress_status: str,
    notify_channel: str,
    video_ids: list[str] | None,
    force: bool,
    *,
    require_file_path: bool = False,
) -> dict:
    """
    video_ids=None means "every row in the table". One UPDATE covering every
    eligible row + one pg_notify — not one per row, since the workers'
    claim_pending already does a broad poll on any wakeup regardless of the
    notify payload.
    """
    if video_ids is not None:
        rows = await conn.fetch(
            f"SELECT video_id, status, file_path FROM {table} WHERE video_id = ANY($1)",
            video_ids,
        )
        not_found = len(set(video_ids) - {r["video_id"] for r in rows})
    else:
        rows = await conn.fetch(f"SELECT video_id, status, file_path FROM {table}")
        not_found = 0

    in_progress = [r["video_id"] for r in rows if r["status"] == in_progress_status]
    not_downloaded = (
        [r["video_id"] for r in rows if r["status"] != in_progress_status and r["file_path"] is None]
        if require_file_path
        else []
    )
    eligible = [
        r["video_id"]
        for r in rows
        if r["status"] != in_progress_status
        and (r["file_path"] is not None or not require_file_path)
        and (force or r["status"] != "done")
    ]
    skipped_done = len(rows) - len(in_progress) - len(not_downloaded) - len(eligible)

    if eligible:
        await conn.execute(
            f"""
            UPDATE {table} SET status='pending', retries=0, error_message=NULL, updated_at=NOW()
            WHERE video_id = ANY($1)
            """,
            eligible,
        )
        await conn.execute(
            "SELECT pg_notify($1, $2)", notify_channel, json.dumps({"event": "bulk_trigger"})
        )

    result = {
        "queued": len(eligible),
        "skipped_in_progress": len(in_progress),
        "skipped_done": skipped_done,
        "not_found": not_found,
    }
    if require_file_path:
        result["skipped_not_downloaded"] = len(not_downloaded)
    return result
