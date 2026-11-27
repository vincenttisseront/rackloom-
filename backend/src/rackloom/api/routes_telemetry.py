"""F23 : administration des agents de télémétrie et ingestion mTLS."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import projects, telemetry
from ..config import get_settings
from ..errors import ApiError
from ..identity.authz import require_admin
from .deps import csrf_guard, unit_of_work

router = APIRouter(tags=["télémétrie"], dependencies=[Depends(csrf_guard)])

# Routeur agent : authentifié par empreinte mTLS, pas de CSRF cookie.
agent_router = APIRouter(tags=["télémétrie-mtls"])


class EnrollBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")
    name: str = Field(min_length=1, max_length=120)
    project: str = Field(min_length=1, max_length=80)


class RevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class LimitsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_payload_bytes: int | None = Field(default=None, ge=64, le=1_048_576)
    quota_bytes_per_window: int | None = Field(default=None, ge=64, le=1_073_741_824)
    window_seconds: int | None = Field(default=None, ge=60, le=86_400)
    retention_days: int | None = Field(default=None, ge=1, le=90)


class IngestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern=r"^(metrics|logs)$")
    # Projet déclaré optionnel : s'il diffère du projet de l'agent → 403 project_spoofed.
    project: str | None = Field(default=None, max_length=80)
    payload: Any


def _fingerprint(request: Request) -> str | None:
    return request.headers.get(telemetry.CERT_HEADER) or request.headers.get("X-Client-Cert-SHA256")


@router.get("/admin/telemetry/agents")
def list_agents(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        return telemetry.list_agents(u.conn, u.p)


@router.post("/admin/telemetry/agents/enroll")
def enroll_agent(request: Request, body: EnrollBody) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        # Admin plateforme (D7) : pas de rôle projet requis pour enrôler un agent.
        project = u.conn.execute(
            "SELECT id, slug FROM projects WHERE slug = %s", (body.project,)
        ).fetchone()
        if project is None:
            raise ApiError(404, "not_found", "projet introuvable")
        return telemetry.enroll(
            u.conn,
            u.p,
            slug=body.slug,
            name=body.name,
            project_id=project["id"],
            keys_dir=get_settings().keys_dir,
            request_id=u.request_id,
        )


@router.post("/admin/telemetry/agents/{agent_id}/revoke")
def revoke_agent(agent_id: UUID, request: Request, body: RevokeBody) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        return telemetry.revoke(u.conn, u.p, agent_id, reason=body.reason, request_id=u.request_id)


@router.put("/admin/telemetry/projects/{project}/limits")
def put_limits(project: str, request: Request, body: LimitsBody) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        require_admin(u.p)
        p = u.conn.execute("SELECT id FROM projects WHERE slug = %s", (project,)).fetchone()
        if p is None:
            raise ApiError(404, "not_found", "projet introuvable")
        return telemetry.set_limits(
            u.conn,
            u.p,
            p["id"],
            max_payload_bytes=body.max_payload_bytes,
            quota_bytes_per_window=body.quota_bytes_per_window,
            window_seconds=body.window_seconds,
            retention_days=body.retention_days,
        )


@router.get("/projects/{project}/telemetry")
def project_telemetry(project: str, request: Request, kind: str | None = None) -> dict[str, Any]:
    with unit_of_work(request) as u:
        p = projects.get_project(u.conn, u.p, project)
        return telemetry.list_samples(u.conn, u.p, p["id"], kind=kind)


@agent_router.post("/telemetry/ingest")
def ingest(request: Request, body: IngestBody = Body(...)) -> dict[str, Any]:
    """L'agent pousse métriques ou journaux (mTLS). Le projet authentifié l'emporte."""
    with unit_of_work(request, write=True, anonymous=True) as u:
        agent = telemetry.authenticate_agent(u.conn, _fingerprint(request))
        return telemetry.ingest(
            u.conn,
            agent,
            kind=body.kind,
            payload=body.payload,
            declared_project=body.project,
            request_id=u.request_id,
        )
