"""Amorçage, connexion, déconnexion, session courante, réauthentification (F12), OIDC (lot 4)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from ..config import get_settings
from ..errors import ApiError
from ..identity import emergency, oidc, reauth
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


class ReauthBody(Strict):
    password: str | None = Field(default=None, max_length=500)
    idp_proof: dict[str, Any] | None = None


def _set_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        identity.cookie_name(),
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
        max_age=settings.session_absolute_minutes * 60,
    )


def _session_body(unit_principal) -> dict[str, Any]:
    p = unit_principal
    return {
        "user": {"id": str(p.user_id), "email": p.email, "display_name": p.display_name},
        "roles": [
            {
                "role": b.role,
                "project_id": str(b.project_id) if b.project_id else None,
                "environment_id": str(b.environment_id) if b.environment_id else None,
                "source": getattr(b, "source", "permanent"),
            }
            for b in p.bindings
        ],
        "csrf_token": p.csrf_token,
        "auth_method": p.auth_method,
        "auth_time": p.auth_time.isoformat(),
        "acr": getattr(p, "acr", "pwd"),
        "amr": list(getattr(p, "amr", ["pwd"]) or ["pwd"]),
    }


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


@router.post("/auth/reauth", dependencies=[Depends(csrf_guard)])
def reauthenticate(body: ReauthBody, request: Request) -> dict[str, Any]:
    """F12 : preuve fraîche (mot de passe local ou IdP de test) pour actions sensibles."""
    with unit_of_work(request, write=True) as u:
        proof = reauth.step_up(
            u.conn,
            u.p,
            password=body.password,
            idp_proof=body.idp_proof,
            request_id=u.request_id,
        )
        token = session_token(request)
        assert token is not None
        principal = identity.resolve_session(u.conn, token, touch=False)
        assert principal is not None
        return {**_session_body(principal), **proof}


@router.get("/auth/oidc")
def oidc_public_status(request: Request) -> dict[str, Any]:
    """État SSO public (page de connexion) : pas de secret."""
    with unit_of_work(request, anonymous=True) as u:
        return oidc.public_view(u.conn)


@router.get("/auth/oidc/login")
def oidc_login(
    request: Request,
    return_to: str | None = Query(default=None, max_length=500),
) -> RedirectResponse:
    """Démarre le flux OIDC (code + PKCE) ; redirige vers l'IdP."""
    with unit_of_work(request, anonymous=True) as u:
        flow = oidc.begin_flow(u.conn, purpose="login", return_to=return_to)
    return RedirectResponse(flow["authorization_url"], status_code=302)


@router.post("/auth/oidc/reauth", dependencies=[Depends(csrf_guard)])
def oidc_reauth(
    request: Request,
    return_to: str | None = Query(default=None, max_length=500),
) -> dict[str, Any]:
    """F12 + OIDC : démarre un step-up IdP (`prompt=login`, `max_age=0`, `acr_values`)."""
    with unit_of_work(request, write=True) as u:
        if not oidc.enabled(u.conn):
            raise ApiError(404, "oidc_disabled", "SSO OIDC non configuré ou désactivé")
        flow = oidc.begin_flow(
            u.conn,
            purpose="reauth",
            session_id=u.p.session_id,
            return_to=return_to,
        )
        return {"authorization_url": flow["authorization_url"]}


@router.get("/auth/oidc/callback")
def oidc_callback(
    request: Request,
    response: Response,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> RedirectResponse:
    """Callback OIDC : échange le code, pose le cookie (login) ou élève la session (reauth)."""
    if error:
        # Ne pas renvoyer error_description brut (peut être verbeux) ; code seulement.
        q = urlencode({"oidc_error": error[:80]})
        return RedirectResponse(f"/connexion?{q}", status_code=302)
    if not code or not state:
        return RedirectResponse("/connexion?oidc_error=missing_code", status_code=302)

    try:
        with unit_of_work(request, anonymous=True) as u:
            session, meta = oidc.complete_callback(
                u.conn,
                code=code,
                state=state,
                previous_token=session_token(request),
            )
            return_to = oidc.safe_return_to(meta.get("return_to"))
            if meta["purpose"] == "login":
                assert session is not None
                redirect = RedirectResponse(return_to, status_code=302)
                _set_cookie(redirect, session.token)
                return redirect
            # reauth : cookie déjà présent ; retour à l'UI.
            return RedirectResponse(return_to, status_code=302)
    except ApiError as exc:
        q = urlencode({"oidc_error": exc.code})
        return RedirectResponse(f"/connexion?{q}", status_code=302)


@router.post("/auth/logout", dependencies=[Depends(csrf_guard)])
def logout(request: Request, response: Response) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        identity.logout(u.conn, u.p)
    response.delete_cookie(
        identity.cookie_name(),
        path="/",
        secure=get_settings().cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return {"ok": True}


@router.get("/session")
def session(request: Request) -> dict[str, Any]:
    with unit_of_work(request, anonymous=True) as u:
        principal = authenticate(u.conn, request, write=False)
        body = _session_body(principal)
        body["sso_available"] = emergency.sso_available(u.conn)
        body["oidc"] = oidc.public_view(u.conn)
        return body
