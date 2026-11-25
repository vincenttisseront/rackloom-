"""Environnements : état, gel et dégel d'urgence (F28).

Geler suspend le démarrage des jobs d'écriture de l'environnement ; la lecture (inventaire,
collecte, vérification, diagnostic) reste disponible. Le contrôle effectif est la règle
d'admission R080 évaluée par le worker, jamais seulement cette route.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request

from .. import workflow
from ..errors import ApiError, forbidden
from ..projects import freezes
from .deps import csrf_guard, idempotent, unit_of_work
from .views import environment_view

router = APIRouter(tags=["environnements"], dependencies=[Depends(csrf_guard)])


def _only(body: dict[str, Any], allowed: set[str]) -> None:
    extra = set(body) - allowed
    if extra:
        key = sorted(extra)[0]
        raise ApiError(422, "unknown_field", f"champ « {key} » refusé", field=f"/{key}")


@router.get("/environments/{environment_id}")
def get_environment(environment_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        env = freezes.load(u.conn, u.p, environment_id)
        return environment_view(u.conn, u.p, env, history=True)


def _toggle(environment_id: UUID, request: Request, body: dict[str, Any], action: str) -> dict[str, Any]:
    _only(body, {"reason"})
    with unit_of_work(request, write=True) as u:
        env = freezes.load(u.conn, u.p, environment_id)

        def act() -> dict[str, Any]:
            workflow.require_action(u.conn, u.p, "environment", env, action)
            apply = freezes.freeze if action == "freeze" else freezes.unfreeze
            apply(u.conn, u.p, environment_id, body.get("reason"), u.request_id)
            return environment_view(u.conn, u.p, freezes.load(u.conn, u.p, environment_id), history=True)

        return idempotent(u, request, body, act)


@router.post("/environments/{environment_id}/freeze")
def freeze(environment_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _toggle(environment_id, request, body, "freeze")


@router.post("/environments/{environment_id}/unfreeze")
def unfreeze(environment_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _toggle(environment_id, request, body, "unfreeze")


@router.get("/admin/environments")
def admin_environments(request: Request) -> list[dict[str, Any]]:
    """Tous les environnements, pour geler d'urgence sans rôle sur le projet (administrateur, auditeur en lecture)."""
    with unit_of_work(request) as u:
        if not (u.p.is_platform_admin or u.p.is_global_auditor):
            raise forbidden("rôle administrateur de plateforme ou auditeur requis")
        rows = u.conn.execute(
            "SELECT e.*, p.slug AS project_slug, p.name AS project_name FROM environments e "
            "JOIN projects p ON p.id = e.project_id ORDER BY p.slug, e.kind = 'prod', e.slug").fetchall()
        return [environment_view(u.conn, u.p, env) for env in rows]
