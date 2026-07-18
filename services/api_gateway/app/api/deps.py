from fastapi import Request
import asyncpg


async def get_db_conn(request: Request) -> asyncpg.Connection:
    """Yield a connection from the pool. FastAPI handles cleanup via context."""
    async with request.app.state.pool.acquire() as conn:
        yield conn
