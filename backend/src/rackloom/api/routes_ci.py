"""F35 : soumission et suivi CI pour identités de service.

Même schéma de besoin et même résolveur que la WebUI. Une seule `Idempotency-Key`
couvre la création de la demande et de sa première version. Aucune route d'approbation.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ..changes import service
from ..errors import ApiError
from ..identity.authz import require_role
from ..projects import get_project
from .deps import csrf_guard, idempotent, unit_of_work
from .views import change_view

router = APIRouter(tags=["ci"], dependencies=[Depends(csrf_guard)])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CiSubmitBody(Strict):
    project: str = Field(min_length=2, max_length=40)
    title: str = Field(default="Demande CI", max_length=200)
    need: dict[str, Any]
    provenance: dict[str, Any] = Field(default_factory=dict)
    submit: bool = False


@router.post("/ci/submissions")
def ci_submit(body: CiSubmitBody, request: Request) -> dict[str, Any]:
    """Soumet un besoin catégorie A. Rejeu avec la même clé → même demande."""
    with unit_of_work(request, write=True) as u:
        if not u.p.is_service:
            raise ApiError(403, "service_identity_required", "route réservée aux identités de service")
        if not request.headers.get("idempotency-key"):
            raise ApiError(422, "idempotency_key_required", "Idempotency-Key obligatoire pour une soumission CI")

        def action() -> dict[str, Any]:
            project = get_project(u.conn, u.p, body.project)
            require_role(u.p, "operator", project["id"])
            if u.p.service_project_id is not None and u.p.service_project_id != project["id"]:
                raise ApiError(403, "project_out_of_scope", "identité de service limitée à un autre projet")
            change = service.create_change(u.conn, u.p, project, body.title)
            version = service.add_version(
                u.conn,
                u.p,
                change["id"],
                {
                    "need": body.need,
                    "provenance": body.provenance,
                    "base_version_no": 0,
                    "source": "ci",
                },
            )
            result: dict[str, Any] = {
                "change_id": str(change["id"]),
                "version_no": version["version_no"],
                "revision_no": version["revision_no"],
                "outcome": version["revision"]["outcome"],
                "change": change_view(u.conn, u.p, version["change"]),
            }
            if body.submit and version["revision"]["outcome"] == "resolved":
                sub = service.submit(u.conn, u.p, change["id"], version["revision_no"])
                result["job_id"] = str(sub["job_id"])
                result["deployment_id"] = str(sub["deployment_id"])
            return result

        return idempotent(u, request, body.model_dump(), action)


@router.get("/ci/submissions/{change_id}")
def ci_get(change_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        if not u.p.is_service:
            raise ApiError(403, "service_identity_required", "route réservée aux identités de service")
        change = service.get_change(u.conn, u.p, change_id)
        if u.p.service_project_id is not None and change["project_id"] != u.p.service_project_id:
            raise ApiError(404, "not_found", "demande introuvable")
        return change_view(u.conn, u.p, change)
