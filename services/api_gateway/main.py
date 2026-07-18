from contextlib import asynccontextmanager

import asyncpg
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.config import Settings
from core.logging import configure_logging
from services.api_gateway.app.api.routes import account, clap, downloads, librosa, mert, playlists, tracks

settings = Settings()
configure_logging("api-gateway", settings.log_level)
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", database_url=settings.database_url.split("@")[-1])
    app.state.pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.db_min_pool_size,
        max_size=settings.db_max_pool_size,
    )
    log.info("db_pool_ready")
    yield
    await app.state.pool.close()
    log.info("shutdown")


app = FastAPI(
    title="YTune API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.error("unhandled_exception", path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred"}},
    )


app.include_router(account.router,   prefix="/v1/account",   tags=["account"])
app.include_router(playlists.router, prefix="/v1/playlists", tags=["playlists"])
app.include_router(tracks.router,    prefix="/v1/tracks",    tags=["tracks"])
app.include_router(downloads.router, prefix="/v1/downloads", tags=["downloads"])
app.include_router(librosa.router,   prefix="/v1/librosa",   tags=["librosa"])
app.include_router(clap.router,      prefix="/v1/clap",      tags=["clap"])
app.include_router(mert.router,      prefix="/v1/mert",      tags=["mert"])


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok", "service": "api-gateway"}
