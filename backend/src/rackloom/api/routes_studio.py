"""Atelier de templates : conception assistée d'un brouillon.

Aucune route ne publie, n'approuve, ni ne crée de job. Publier reste une route d'administration,
liée à l'empreinte affichée, par une autre personne que l'auteur.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request

from .. import studio
from ..errors import ApiError, forbidden
from ..identity.authz import Principal
from .deps import csrf_guard, unit_of_work

router = APIRouter(tags=["atelier"], dependencies=[Depends(csrf_guard)])


def _reader(principal: Principal) -> None:
    if not (
        principal.has_role("template_author") or principal.has_role("template_publisher") or principal.is_platform_admin
    ):
        raise forbidden("rôle auteur ou publicateur de templates requis")


def _turn(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {key: result.get(key) for key in ("status", "code", "notice", "reply", "simulated", "adapter")}


@router.get("/studio/catalog")
def catalog(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        _reader(u.p)
        return studio.catalog(u.conn)


@router.get("/studio/bases")
def bases(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        _reader(u.p)
        return studio.bases(u.conn)


@router.get("/studio/drafts")
def list_drafts(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        _reader(u.p)
        return studio.list_drafts(u.conn, u.p)


@router.post("/studio/drafts")
def create_draft(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        result = studio.create(u.conn, u.p, body)
        return {"draft": studio.view(u.conn, u.p, result["draft"]), "assistant": _turn(result["turn"])}


@router.get("/studio/drafts/{draft_id}")
def get_draft(draft_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        draft = studio.get_visible(u.conn, u.p, draft_id)
        return studio.view(u.conn, u.p, draft)


@router.post("/studio/drafts/{draft_id}/edit")
def edit_draft(draft_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        draft = studio.edit(u.conn, u.p, draft_id, body)
        return studio.view(u.conn, u.p, draft)


@router.post("/studio/drafts/{draft_id}/assist")
def assist(draft_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        result = studio.assist(u.conn, u.p, draft_id, body)
        return {**(_turn(result) or {}), "draft": studio.view(u.conn, u.p, result["draft"])}


@router.post("/studio/drafts/{draft_id}/suggestions/{suggestion_id}")
def decide_suggestion(
    draft_id: UUID, suggestion_id: str, request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    if not suggestion_id or len(suggestion_id) > 40:
        raise ApiError(422, "invalid_suggestion", "identifiant de suggestion invalide")
    with unit_of_work(request, write=True) as u:
        draft = studio.decide_suggestion(u.conn, u.p, draft_id, suggestion_id, body)
        return studio.view(u.conn, u.p, draft)


@router.post("/studio/drafts/{draft_id}/submit")
def submit_draft(draft_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        result = studio.submit(u.conn, u.p, draft_id, body)
        return {
            "slug": result["slug"],
            "version": result["version"],
            "status": result["status"],
            "digest": result["digest"],
            "draft": studio.view(u.conn, u.p, result["draft"]),
        }


@router.post("/studio/drafts/{draft_id}/abandon")
def abandon_draft(draft_id: UUID, request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        draft = studio.abandon(u.conn, u.p, draft_id, body)
        return studio.view(u.conn, u.p, draft)
