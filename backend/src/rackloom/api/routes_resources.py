"""Déploiements, fiches machine, observations et artefacts de classe B."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from .. import artifacts, audit
from ..changes import service
from ..errors import not_found
from .deps import csrf_guard, unit_of_work
from .views import deployment_view, host_view, iso

router = APIRouter(tags=["ressources"], dependencies=[Depends(csrf_guard)])

OBSERVATION_KINDS = ("facts", "packages", "services", "probe", "journal", "ssh")


@router.get("/deployments/{deployment_id}")
def get_deployment(deployment_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        return deployment_view(u.conn, u.p, service.get_deployment(u.conn, u.p, deployment_id))


def _host(conn, principal, host_id: UUID) -> dict[str, Any]:
    host = conn.execute("SELECT * FROM hosts WHERE id = %s", (host_id,)).fetchone()
    if host is None or not principal.can_see_project(host["project_id"]):
        raise not_found("machine")
    return host


@router.get("/hosts/{host_id}")
def get_host(host_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        return host_view(u.conn, _host(u.conn, u.p, host_id))


@router.get("/hosts/{host_id}/observations")
def host_observations(host_id: UUID, request: Request, kind: str | None = None) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        host = _host(u.conn, u.p, host_id)
        if kind is not None and kind not in OBSERVATION_KINDS:
            return []
        rows = u.conn.execute(
            "SELECT id, kind, data, status, source, collected_at FROM host_observations WHERE host_id = %s "
            + ("AND kind = %s " if kind else "") + "ORDER BY collected_at DESC LIMIT 100",
            (host["id"], kind) if kind else (host["id"],)).fetchall()
        return [{**r, "collected_at": iso(r["collected_at"])} for r in rows]


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: UUID, request: Request) -> Response:
    with unit_of_work(request) as u:
        row = u.conn.execute("SELECT * FROM artifacts WHERE id = %s", (artifact_id,)).fetchone()
        if row is None or row["key_class"] != "B" or not u.p.can_see_project(row["project_id"]):
            raise not_found("artefact")
        content = artifacts.read(row)
        audit.record(u.conn, actor_type="user", actor_id=str(u.p.user_id), action="artifact.download",
                     outcome="ok", project_id=row["project_id"], correlation_id=row["change_id"],
                     targets={"artifact": str(artifact_id), "kind": row["kind"]}, request_id=u.request_id)
    return Response(content, media_type="application/json" if row["kind"].endswith("json") else "text/plain",
                    headers={"Content-Disposition": f'attachment; filename="{row["kind"]}-{artifact_id}.txt"',
                             "Cache-Control": "no-store"})
