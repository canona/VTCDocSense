import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI
from redis.asyncio import Redis

from app import __version__
from app.api.routes import health, internal
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import get_engine
from app.providers import build_provider


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.redis = Redis.from_url(settings.redis_url)
    app.state.provider = build_provider(settings)
    pool: list[ArqRedis] = []  # tạo lazy: API vẫn khởi động được khi Redis chưa sẵn sàng

    async def enqueue(doc_id: uuid.UUID) -> None:
        if not pool:
            pool.append(await create_pool(RedisSettings.from_dsn(settings.redis_url)))
        # _job_id cố định theo document -> không xếp hàng trùng
        await pool[0].enqueue_job("process_document", str(doc_id), _job_id=f"doc:{doc_id}")

    app.state.enqueue = enqueue
    try:
        yield
    finally:
        for p in pool:
            await p.aclose()
        await app.state.provider.aclose()
        await app.state.redis.aclose()
        await get_engine().dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)
    docs = settings.enable_docs
    app = FastAPI(
        title="VTCDocSense API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.include_router(health.router)
    app.include_router(internal.router)
    return app


app = create_app()
