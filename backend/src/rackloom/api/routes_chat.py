"""Assistant : conversations et tours validés. Aucune route du chat n'écrit de besoin, d'approbation ni de job."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request

from .. import chat, projects
from ..errors import ApiError
from ..identity.authz import require_admin
from .deps import csrf_guard, unit_of_work

router = APIRouter(tags=["assistant"], dependencies=[Depends(csrf_guard)])


@router.get("/assistant/status")
def assistant_status(request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        cfg = chat.assistant_settings(u.conn)
        return {"available": cfg["available"], "simulated": cfg["simulated"], "adapter": cfg["adapter"]}


@router.put("/admin/assistant")
def configure_assistant(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    extra = set(body) - {"adapter", "scenario"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé")
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        return chat.set_assistant(u.conn, u.p, str(body.get("adapter")), str(body.get("scenario", "nominal")))


@router.post("/projects/{project}/conversations")
def create_conversation(project: str, request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    extra = set(body) - {"change_id"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé")
    try:
        change_id = UUID(str(body["change_id"])) if body.get("change_id") else None
    except ValueError as exc:
        raise ApiError(422, "invalid_change", "identifiant de demande invalide", field="/change_id") from exc
    with unit_of_work(request, write=True) as u:
        p = projects.get_project(u.conn, u.p, project)
        conv = chat.create_conversation(u.conn, u.p, p, change_id)
        cfg = chat.assistant_settings(u.conn)
        return {"id": str(conv["id"]), "assistant": {"available": cfg["available"], "simulated": cfg["simulated"]}}


@router.post("/conversations/{conversation_id}/messages")
def post_message(conversation_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        return chat.post_message(u.conn, u.p, conversation_id, body)
