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
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from . import routers
from .deps import (
    REQUEST_ID_HEADER,
    NoBodyLoggingFilter,
    RequestIdMiddleware,
    close_pool,
    error_detail,
    http_error_detail,
    no_store_headers,
    request_id_of,
    validation_error_detail,
)

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


logger = logging.getLogger("crm.api.error")


def _error_response(detail: dict, status_code: int,
                    headers: dict | None = None) -> JSONResponse:
    """The one error response: the body plus the request-id header."""
    out = dict(headers or {})
    request_id = detail.get("request_id")
    if request_id:
        out[REQUEST_ID_HEADER] = request_id
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder({"detail": detail}),
        headers=out,
    )


def create_app() -> FastAPI:
    app = FastAPI(title="CRM-core API", version="0.1.0", lifespan=lifespan)

    # One error shape for every failure (A120). The handlers below answer
    # with `{"detail": {"code", "message", "field", "request_id"}}` -- the
    # request id is the one minted by RequestIdMiddleware for this request,
    # and it is also in the `x-request-id` header, so a reader who is told
    # to quote a reference can quote one string that leads back to the log.
    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        detail = http_error_detail(
            exc.detail, exc.status_code, request_id=request_id_of(request)
        )
        return _error_response(detail, exc.status_code, dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        detail = validation_error_detail(
            exc.errors(), request_id=request_id_of(request)
        )
        return _error_response(detail, 422)

    @app.exception_handler(Exception)
    async def _unhandled_error(request: Request, exc: Exception):
        # The exception text stays in the log (with the request id); the
        # client gets a sentence and the id, never the traceback.
        request_id = request_id_of(request)
        logger.exception(
            "unhandled error request_id=%s %s %s",
            request_id, request.method, request.url.path,
        )
        detail = error_detail("internal_error", status_code=500, request_id=request_id)
        return _error_response(detail, 500)

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
    # Outermost of the two, so the request id is on EVERY response --
    # including the ones the error handlers build.
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router)
    for router in _discover_routers():
        app.include_router(router, prefix="/api/v1")
    return app


app = create_app()
