"""Unité de travail par requête : authentification, CSRF, idempotence et transaction.

Chaque route ouvre `unit_of_work(request, write=...)`. Les écritures exigent le jeton CSRF de
la session (en-tête `X-CSRF-Token`) et refusent une `Origin` étrangère. La transaction est
validée à la sortie, annulée sur erreur, sauf `ApiError(commit=True)`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder

from ..config import get_settings
from ..db import Conn, Jsonb, get_pool
from ..errors import ApiError
from ..identity import service as identity
from ..identity.authz import Principal
from ..security import constant_time_equals, csrf_token_for

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
IDEMPOTENCY_TTL = timedelta(hours=24)


@dataclass
class Unit:
    conn: Conn
    principal: Principal | None
    request_id: str

    @property
    def p(self) -> Principal:
        assert self.principal is not None
        return self.principal


def session_token(request: Request) -> str | None:
    return request.cookies.get(identity.cookie_name())


def bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token or None
    return None


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") != get_settings().public_origin.rstrip("/"):
        raise ApiError(403, "origin_refused", "origine de la requête refusée")


def csrf_guard(request: Request) -> None:
    """Dépendance des routeurs : vérifie Origin et CSRF avant toute validation du corps.

    Les identités de service (F35, Bearer) n'utilisent pas de cookie : pas de CSRF.
    """
    if request.method not in UNSAFE:
        return
    check_origin(request)
    if bearer_token(request):
        return
    token = session_token(request)
    if not token:
        raise ApiError(401, "unauthenticated", "session absente ou expirée")
    sent = request.headers.get("x-csrf-token", "")
    if not sent or not constant_time_equals(sent, csrf_token_for(token)):
        raise ApiError(403, "csrf_refused", "jeton CSRF absent ou invalide")


def authenticate(conn: Conn, request: Request, *, write: bool) -> Principal:
    from ..identity import service_accounts

    bearer = bearer_token(request)
    if bearer:
        principal = service_accounts.resolve_token(conn, bearer)
        if principal is None:
            raise ApiError(401, "unauthenticated", "jeton de service absent ou révoqué")
        if write or request.method in UNSAFE:
            check_origin(request)
        return principal
    token = session_token(request)
    principal = identity.resolve_session(conn, token) if token else None
    if principal is None:
        raise ApiError(401, "unauthenticated", "session absente ou expirée")
    if write or request.method in UNSAFE:
        check_origin(request)
        sent = request.headers.get("x-csrf-token", "")
        if not sent or not constant_time_equals(sent, principal.csrf_token):
            raise ApiError(403, "csrf_refused", "jeton CSRF absent ou invalide")
    return principal


@contextmanager
def unit_of_work(request: Request, *, write: bool = False, anonymous: bool = False) -> Iterator[Unit]:
    with get_pool().connection() as conn:
        try:
            principal = None if anonymous else authenticate(conn, request, write=write)
            yield Unit(conn=conn, principal=principal, request_id=request.state.request_id)
        except ApiError as exc:
            if exc.commit:
                conn.commit()
            else:
                conn.rollback()
            raise
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()


def idempotent(unit: Unit, request: Request, body: Any, action: Callable[[], Any]) -> Any:
    """Rejoue la réponse enregistrée pour une même `Idempotency-Key` du même utilisateur."""
    key = request.headers.get("idempotency-key")
    if not key:
        return action()
    if len(key) > 200:
        raise ApiError(422, "invalid_idempotency_key", "clé d'idempotence trop longue")
    route = f"{request.method} {request.url.path}"
    digest = hashlib.sha256(json.dumps(jsonable_encoder(body), sort_keys=True).encode()).hexdigest()
    conn = unit.conn
    inserted = conn.execute(
        "INSERT INTO idempotency_keys (principal_id, key, route, request_sha256, expires_at) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING key",
        (unit.p.user_id, key, route, digest, datetime.now(UTC) + IDEMPOTENCY_TTL)).fetchone()
    if inserted is None:
        row = conn.execute("SELECT * FROM idempotency_keys WHERE principal_id = %s AND key = %s",
                           (unit.p.user_id, key)).fetchone()
        if row["route"] != route or row["request_sha256"] != digest:
            raise ApiError(422, "idempotency_key_reused", "clé d'idempotence déjà utilisée pour une autre requête")
        if row["response"] is None:
            raise ApiError(409, "idempotency_in_progress", "requête identique en cours de traitement")
        return row["response"]
    result = jsonable_encoder(action())
    conn.execute("UPDATE idempotency_keys SET status_code = 200, response = %s WHERE principal_id = %s AND key = %s",
                 (Jsonb(result), unit.p.user_id, key))
    return result
