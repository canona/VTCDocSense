from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app import __version__
from app.api.routes import health
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import get_engine
from app.providers import build_provider


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.redis = Redis.from_url(settings.redis_url)
    app.state.provider = build_provider(settings)
    try:
        yield
    finally:
        await app.state.provider.aclose()
        await app.state.redis.aclose()
        await get_engine().dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)
    docs = settings.enable_docs
    app = FastAPI(
        title="GP-OCR API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.include_router(health.router)
    return app


app = create_app()
