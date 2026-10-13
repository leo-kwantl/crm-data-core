"""FastAPI factory. AUTO-MOUNTS every module in routers/ exposing a `router` object.

Contract for later agents: to add endpoints, ADD a new file
services/api/routers/<resource>.py exposing `router = APIRouter(...)`.
NEVER edit this file to register routes — discovery is automatic via pkgutil.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import routers
from .deps import NoBodyLoggingFilter, close_pool, no_store_headers

for _handler in logging.root.handlers:
    _handler.addFilter(NoBodyLoggingFilter())
logging.getLogger("uvicorn.access").addFilter(NoBodyLoggingFilter())


def _discover_routers() -> list:
    found = []
    for mod in pkgutil.iter_modules(routers.__path__):
        module = importlib.import_module(f"{__name__.rsplit('.', 1)[0]}.routers.{mod.name}")
        router = getattr(module, "router", None)
        if router is not None:
            found.append(router)
    return found


health_router = APIRouter()


@health_router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await close_pool()


def create_app() -> FastAPI:
    app = FastAPI(title="CRM-core API", version="0.1.0", lifespan=lifespan)
    # Browser UI (apps/web on :3000) calls this API cross-origin; allow it.
    # Extra origins via WEB_ORIGINS env (comma-separated). Keep tight: no "*".
    import os

    origins = [o.strip() for o in os.getenv(
        "WEB_ORIGINS", "http://localhost:3000").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["content-type", "x-session-token"],
        max_age=600,
    )
    app.middleware("http")(no_store_headers)
    app.include_router(health_router)
    for router in _discover_routers():
        app.include_router(router, prefix="/api/v1")
    return app


app = create_app()
