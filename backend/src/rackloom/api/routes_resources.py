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


@router.get("/inbox")
def get_inbox(request: Request) -> dict[str, Any]:
    """F55 : boîte « À moi d'agir » et « En attente d'une autre personne »."""
    from .. import inbox

    with unit_of_work(request) as u:
        return inbox.build(u.conn, u.p)


@router.get("/examples")
def get_examples(request: Request) -> dict[str, Any]:
    """F51 : exemples « qu'est-ce que je peux demander » (tous projets visibles)."""
    from .. import examples

    with unit_of_work(request) as u:
        return examples.build(u.conn, u.p)


@router.get("/deployments/{deployment_id}")
def get_deployment(deployment_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        return deployment_view(u.conn, u.p, service.get_deployment(u.conn, u.p, deployment_id))


@router.get("/deployments/{deployment_id}/service-card")
def get_service_card(deployment_id: UUID, request: Request) -> dict[str, Any]:
    """F57 : fiche courte « Votre service est prêt » (périmètre projet, sorties allowlist)."""
    from ..deployments import service_card

    with unit_of_work(request) as u:
        return service_card.for_principal(u.conn, u.p, deployment_id)


@router.get("/deployments/{deployment_id}/drift")
def get_drift(deployment_id: UUID, request: Request) -> dict[str, Any]:
    """F09 : dernier scan de dérive (lecture seule, aucune correction)."""
    from .. import drift

    with unit_of_work(request) as u:
        dep = service.get_deployment(u.conn, u.p, deployment_id)
        latest = drift.latest_for_deployment(u.conn, dep["id"])
        if latest is None:
            return {"status": "none", "findings": [], "correction": False,
                    "notice": "Aucun scan de dérive pour ce déploiement."}
        return latest


@router.post("/deployments/{deployment_id}/drift/scan")
def trigger_drift_scan(deployment_id: UUID, request: Request) -> dict[str, Any]:
    """F09 : enfile un job de lecture `drift_scan` (aucune écriture fournisseur)."""
    from .. import drift
    from ..config import get_settings
    from ..executions import queue

    with unit_of_work(request, write=True) as u:
        dep = service.get_deployment(u.conn, u.p, deployment_id)
        # Scan synchrone pour l'API admin/opérateur (preuve locale) ; job aussi enfilé pour l'audit.
        result = drift.scan_deployment(
            u.conn,
            deployment_id=dep["id"],
            workspaces_parent=get_settings().data_dir,
            actor_type="user",
            actor_id=str(u.p.user_id),
        )
        job = queue.enqueue(
            u.conn,
            kind="drift_scan",
            change=None,
            project_id=dep["project_id"],
            stack_id=dep.get("stack_id"),
            idempotency_key=f"drift_scan:{dep['id']}:{result['scan_id']}",
            payload={"deployment_id": str(dep["id"]), "already_scanned": result["scan_id"]},
        )
        return {**result, "job_id": str(job["id"])}


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
