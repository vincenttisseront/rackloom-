"""Amorçage, connexion, déconnexion, session courante."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..config import get_settings
from ..errors import ApiError
from ..identity import service as identity
from .deps import authenticate, check_origin, csrf_guard, session_token, unit_of_work

router = APIRouter(tags=["auth"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(Strict):
    email: str = Field(max_length=200)
    password: str = Field(max_length=500)


class BootstrapBody(Strict):
    token: str = Field(max_length=200)
    email: str = Field(max_length=200)
    display_name: str = Field(max_length=120)
    password: str = Field(max_length=500)


def _set_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(identity.cookie_name(), token, httponly=True, secure=settings.cookie_secure,
                        samesite="strict", path="/", max_age=settings.session_absolute_minutes * 60)


def _session_body(unit_principal) -> dict[str, Any]:
    p = unit_principal
    return {"user": {"id": str(p.user_id), "email": p.email, "display_name": p.display_name},
            "roles": [{"role": b.role, "project_id": str(b.project_id) if b.project_id else None,
                       "environment_id": str(b.environment_id) if b.environment_id else None} for b in p.bindings],
            "csrf_token": p.csrf_token, "auth_method": p.auth_method, "auth_time": p.auth_time.isoformat()}


@router.post("/auth/bootstrap")
def bootstrap(body: BootstrapBody, request: Request) -> dict[str, Any]:
    check_origin(request)
    with unit_of_work(request, anonymous=True) as u:
        user_id = identity.consume_bootstrap(u.conn, body.token, body.email, body.display_name, body.password)
    return {"user_id": str(user_id)}


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    check_origin(request)
    client = request.client.host if request.client else "inconnu"
    with unit_of_work(request, anonymous=True) as u:
        result = identity.login(u.conn, body.email, body.password, client, previous_token=session_token(request))
        if isinstance(result, ApiError):
            result.commit = True
            raise result
        principal = identity.resolve_session(u.conn, result.token, touch=False)
    _set_cookie(response, result.token)
    return _session_body(principal)


@router.post("/auth/logout", dependencies=[Depends(csrf_guard)])
def logout(request: Request, response: Response) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        identity.logout(u.conn, u.p)
    response.delete_cookie(identity.cookie_name(), path="/", secure=get_settings().cookie_secure, httponly=True,
                           samesite="strict")
    return {"ok": True}


@router.get("/session")
def session(request: Request) -> dict[str, Any]:
    with unit_of_work(request, anonymous=True) as u:
        principal = authenticate(u.conn, request, write=False)
        return _session_body(principal)
