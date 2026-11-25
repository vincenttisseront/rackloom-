"""Application FastAPI : préfixe /api/v1, erreurs normalisées, en-têtes de sécurité, identifiant de requête."""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..errors import ApiError
from . import (
    routes_admin,
    routes_auth,
    routes_changes,
    routes_chat,
    routes_follow,
    routes_projects,
    routes_resources,
)

log = logging.getLogger(__name__)

PREFIX = "/api/v1"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
NO_STORE = "no-store"


def _pointer(loc: tuple[Any, ...]) -> str:
    parts = [str(p) for p in loc if p not in ("body", "query", "path")]
    return "/" + "/".join(parts)


def create_app() -> FastAPI:
    app = FastAPI(title="Rackloom", version="0.1.0", openapi_url=f"{PREFIX}/openapi.json",
                  docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        request.state.request_id = incoming if incoming.isalnum() and len(incoming) <= 64 else secrets.token_hex(8)
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith(PREFIX):
            response.headers.setdefault("Cache-Control", NO_STORE)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.to_dict(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {"loc": (), "msg": "requête invalide"}
        body = {"code": "invalid_request", "message": "requête invalide", "field": _pointer(tuple(first["loc"]))}
        if first.get("type") == "extra_forbidden":
            body["code"] = "unknown_field"
            body["message"] = f"champ « {first['loc'][-1]} » refusé"
        return JSONResponse(body, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse({"code": code, "message": str(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("erreur inattendue", extra={"request_id": getattr(request.state, "request_id", None)})
        return JSONResponse({"code": "internal_error", "message": "erreur interne",
                             "details": {"request_id": getattr(request.state, "request_id", None)}},
                            status_code=500)

    for module in (routes_auth, routes_projects, routes_changes, routes_follow, routes_resources, routes_chat,
                   routes_admin):
        app.include_router(module.router, prefix=PREFIX)
    app.include_router(routes_admin.health, prefix=PREFIX)
    return app


app = create_app()
