"""Déploiements, fiches machine, observations et artefacts de classe B."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import Response

from .. import artifacts, audit, workflow
from ..changes import service
from ..errors import ApiError, not_found
from .deps import csrf_guard, idempotent, unit_of_work
from .views import change_view, deployment_view, host_view, iso

router = APIRouter(tags=["ressources"], dependencies=[Depends(csrf_guard)])


def _only(body: dict[str, Any], allowed: set[str]) -> None:
    extra = set(body) - allowed
    if extra:
        key = sorted(extra)[0]
        raise ApiError(422, "unknown_field", f"champ « {key} » refusé", field=f"/{key}")


OBSERVATION_KINDS = ("facts", "packages", "services", "probe", "journal", "ssh", "settings")


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


@router.get("/deployments/{deployment_id}/lease")
def get_deployment_lease(deployment_id: UUID, request: Request) -> dict[str, Any]:
    """F06 : bail du déploiement (durée de vie, propriétaire, état)."""
    from .. import leases

    with unit_of_work(request) as u:
        lease = leases.for_deployment(u.conn, u.p, deployment_id)
        if lease is None:
            raise not_found("bail")
        return lease


@router.post("/deployments/{deployment_id}/lease/renew")
def renew_deployment_lease(
    deployment_id: UUID, request: Request, body: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """F06 : renouveler un bail (aucune écriture fournisseur)."""
    from .. import leases

    _only(body, {"expires_on", "days", "reason"})
    with unit_of_work(request, write=True) as u:

        def act() -> dict[str, Any]:
            return leases.renew(u.conn, u.p, deployment_id, body, request_id=u.request_id)

        return idempotent(u, request, body, act)


@router.post("/deployments/{deployment_id}/lease/propose-retirement")
def propose_lease_retirement(
    deployment_id: UUID, request: Request, body: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """F06 : proposer un plan de retrait (labo intact jusqu'à approbation + exécution)."""
    from .. import leases

    _only(body, {"reason"})
    with unit_of_work(request, write=True) as u:

        def act() -> dict[str, Any]:
            reason = str(body.get("reason") or "Plan de retrait demandé par l'opérateur")
            result = leases.propose_retirement(
                u.conn, u.p, deployment_id, reason=reason, request_id=u.request_id, auto=False
            )
            change_id = result.get("change_id")
            change = service.get_change(u.conn, u.p, UUID(change_id)) if change_id else None
            return {
                **{k: v for k, v in result.items() if k != "change"},
                "change": change_view(u.conn, u.p, change) if change else None,
            }

        return idempotent(u, request, body, act)


@router.post("/deployments/{deployment_id}/actions/{action_id}")
def start_service_action(
    deployment_id: UUID, action_id: str, request: Request, body: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """F58 : ouvre une demande préremplie (capacité, redémarrage) — jamais une commande directe."""
    from ..deployments import actions as service_actions

    _only(body, set())
    with unit_of_work(request, write=True) as u:
        dep = service.get_deployment(u.conn, u.p, deployment_id)

        def act() -> dict[str, Any]:
            # require_action est aussi appelé dans start ; double contrôle explicite ici.
            workflow.require_action(u.conn, u.p, "deployment", dep, action_id)
            result = service_actions.start(u.conn, u.p, deployment_id, action_id, request_id=u.request_id)
            change = result["change"]
            return {
                **{k: v for k, v in result.items() if k != "change"},
                "change": change_view(u.conn, u.p, change),
            }

        return idempotent(u, request, body, act)


@router.get("/deployments/{deployment_id}/access-requests")
def list_access_requests(deployment_id: UUID, request: Request) -> dict[str, Any]:
    """F59 : demandes d'accès du déploiement."""
    from .. import access

    with unit_of_work(request) as u:
        return {
            "items": access.list_for_deployment(u.conn, u.p, deployment_id),
            "levels": access.levels_available(u.conn, service.get_deployment(u.conn, u.p, deployment_id)),
        }


@router.post("/deployments/{deployment_id}/access-requests")
def create_access_request(
    deployment_id: UUID, request: Request, body: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """F59 : demander un niveau d'accès (groupe résolu côté serveur)."""
    from .. import access

    with unit_of_work(request, write=True) as u:

        def act() -> dict[str, Any]:
            return access.create_request(u.conn, u.p, deployment_id, body, request_id=u.request_id)

        return idempotent(u, request, body, act)


@router.get("/access-requests/{request_id}")
def get_access_request(request_id: UUID, request: Request) -> dict[str, Any]:
    from .. import access

    with unit_of_work(request) as u:
        return access.get_request(u.conn, u.p, request_id)


@router.post("/access-requests/{request_id}/approve")
def approve_access_request(
    request_id: UUID, request: Request, body: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    from .. import access

    _only(body, set())
    with unit_of_work(request, write=True) as u:

        def act() -> dict[str, Any]:
            return access.approve(u.conn, u.p, request_id, request_id_http=u.request_id)

        return idempotent(u, request, body, act)


@router.post("/access-requests/{request_id}/reject")
def reject_access_request(
    request_id: UUID, request: Request, body: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    from .. import access

    _only(body, {"comment"})
    with unit_of_work(request, write=True) as u:

        def act() -> dict[str, Any]:
            return access.reject(
                u.conn,
                u.p,
                request_id,
                comment=str(body.get("comment") or ""),
                request_id_http=u.request_id,
            )

        return idempotent(u, request, body, act)


@router.post("/access-requests/{request_id}/retry-sync")
def retry_access_sync(request_id: UUID, request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """F59 : relance la sync IdP avec la même clé d'idempotence (pas de doublon)."""
    from .. import access

    _only(body, set())
    with unit_of_work(request, write=True) as u:

        def act() -> dict[str, Any]:
            return access.sync_to_idp(u.conn, u.p, request_id, request_id_http=u.request_id)

        return idempotent(u, request, body, act)


@router.get("/deployments/{deployment_id}/outputs")
def get_deployment_outputs(deployment_id: UUID, request: Request) -> dict[str, Any]:
    """F33 : sorties typées versionnées (allowlist) ; hors périmètre → 404 ; jamais le state."""
    from ..deployments import outputs as deployment_outputs

    with unit_of_work(request) as u:
        return deployment_outputs.latest(u.conn, u.p, deployment_id)


@router.get("/deployments/{deployment_id}/availability")
def get_availability(deployment_id: UUID, request: Request) -> dict[str, Any]:
    """F18 : disponibilité — état du service, santé de la sonde et fraîcheur distincts."""
    from .. import probes

    with unit_of_work(request) as u:
        # get_deployment applique le périmètre (404 hors projet).
        service.get_deployment(u.conn, u.p, deployment_id)
        return probes.availability_report(u.conn, u.p, deployment_id)


@router.post("/deployments/{deployment_id}/probes/run")
def run_deployment_probe(
    deployment_id: UUID, request: Request, body: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """F18 : lance une sonde depuis le segment prévu (aucune écriture fournisseur)."""
    from .. import probes

    with unit_of_work(request, write=True) as u:
        dep = service.get_deployment(u.conn, u.p, deployment_id)
        _only(body, {"segment", "force_probe_ok", "service_state"})
        force_ok = body.get("force_probe_ok")
        if force_ok is not None and not isinstance(force_ok, bool):
            raise ApiError(422, "invalid_request", "force_probe_ok doit être un booléen")
        service_state = body.get("service_state")
        if service_state is not None and service_state not in ("active", "inactive", "unknown"):
            raise ApiError(422, "invalid_request", "service_state invalide")
        return probes.run_check(
            u.conn,
            dep=dep,
            actor_type="user",
            actor_id=str(u.p.user_id),
            force_segment=body.get("segment"),
            force_probe_ok=force_ok,
            service_state_override=service_state,
        )


@router.get("/deployments/{deployment_id}/drift")
def get_drift(deployment_id: UUID, request: Request) -> dict[str, Any]:
    """F09 : dernier scan de dérive (lecture seule, aucune correction)."""
    from .. import drift

    with unit_of_work(request) as u:
        dep = service.get_deployment(u.conn, u.p, deployment_id)
        latest = drift.latest_for_deployment(u.conn, dep["id"])
        if latest is None:
            return {
                "status": "none",
                "findings": [],
                "correction": False,
                "notice": "Aucun scan de dérive pour ce déploiement.",
            }
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


@router.get("/patch-campaigns/{campaign_id}")
def get_patch_campaign(campaign_id: UUID, request: Request) -> dict[str, Any]:
    """F19 : détail d'une campagne de patch (vagues, contrôles, écritures)."""
    from .. import patching

    with unit_of_work(request) as u:
        return patching.get_campaign(u.conn, u.p, campaign_id)


@router.get("/adoptions/{adoption_id}")
def get_adoption(adoption_id: UUID, request: Request) -> dict[str, Any]:
    """F08 : détail d'une adoption (observation fictive, proposition, état)."""
    from .. import adoption

    with unit_of_work(request) as u:
        return adoption.get(u.conn, u.p, adoption_id)


@router.post("/adoptions/{adoption_id}/observe")
def observe_adoption(adoption_id: UUID, request: Request) -> dict[str, Any]:
    """F08 : rafraîchit l'observation fictive (lecture seule)."""
    from .. import adoption

    with unit_of_work(request, write=True) as u:
        return adoption.observe(u.conn, u.p, adoption_id)


@router.post("/adoptions/{adoption_id}/preview")
def preview_adoption_import(adoption_id: UUID, request: Request) -> dict[str, Any]:
    """F08 : aperçu d'import Terraform — ne modifie pas la VM."""
    from .. import adoption

    with unit_of_work(request, write=True) as u:
        return adoption.preview_import(u.conn, u.p, adoption_id)


@router.post("/adoptions/{adoption_id}/propose-import")
def propose_adoption_import(adoption_id: UUID, request: Request) -> dict[str, Any]:
    """F08 : enregistre la proposition d'import (séparée de l'exécution)."""
    from .. import adoption

    with unit_of_work(request, write=True) as u:
        return adoption.propose_import(u.conn, u.p, adoption_id)


@router.post("/adoptions/{adoption_id}/approve")
def approve_adoption_import(adoption_id: UUID, request: Request) -> dict[str, Any]:
    """F08 : approuve la proposition figée (empreinte)."""
    from .. import adoption

    with unit_of_work(request, write=True) as u:
        return adoption.approve_import(u.conn, u.p, adoption_id)


@router.post("/adoptions/{adoption_id}/import")
def execute_adoption_import(adoption_id: UUID, request: Request) -> dict[str, Any]:
    """F08 : exécute l'import simulé ; conflit de propriété → 409."""
    from .. import adoption

    with unit_of_work(request, write=True) as u:
        return adoption.execute_import(u.conn, u.p, adoption_id)


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
            + ("AND kind = %s " if kind else "")
            + "ORDER BY collected_at DESC LIMIT 100",
            (host["id"], kind) if kind else (host["id"],),
        ).fetchall()
        return [{**r, "collected_at": iso(r["collected_at"])} for r in rows]


@router.get("/compliance/profiles")
def list_compliance_profiles(request: Request) -> list[dict[str, Any]]:
    """F24 : profils de conformité publiés (versionnés, immuables)."""
    from .. import compliance

    with unit_of_work(request) as u:
        return compliance.list_profiles(u.conn)


@router.get("/hosts/{host_id}/compliance")
def get_host_compliance(host_id: UUID, request: Request) -> dict[str, Any]:
    """F24 : dernier scan de conformité (lecture seule, aucune correction)."""
    from .. import compliance

    with unit_of_work(request) as u:
        _host(u.conn, u.p, host_id)
        latest = compliance.latest_for_host(u.conn, host_id)
        if latest is None:
            return {
                "status": "none",
                "findings": [],
                "correction": False,
                "notice": "Aucun scan de conformité pour cette machine.",
            }
        return latest


@router.post("/hosts/{host_id}/compliance/scan")
def scan_host_compliance(
    host_id: UUID, request: Request, body: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """F24 : scan synchrone + job `compliance_scan` (classe read, aucune écriture)."""
    from .. import compliance
    from ..executions import queue

    _only(body, {"profile_slug", "profile_version", "source"})
    with unit_of_work(request, write=True) as u:
        host = _host(u.conn, u.p, host_id)
        result = compliance.scan_host(
            u.conn,
            host_id=host["id"],
            principal=u.p,
            profile_slug=str(body.get("profile_slug") or "ubuntu-web-baseline"),
            profile_version=body.get("profile_version"),
            source=str(body.get("source") or "rackloom"),
            actor_type="user",
            actor_id=str(u.p.user_id),
        )
        job = queue.enqueue(
            u.conn,
            kind="compliance_scan",
            change=None,
            project_id=host["project_id"],
            stack_id=None,
            idempotency_key=f"compliance_scan:{host['id']}:{result['scan_id']}",
            payload={
                "host_id": str(host["id"]),
                "already_scanned": result["scan_id"],
                "profile_slug": result.get("profile", {}).get("slug"),
            },
        )
        return {**result, "job_id": str(job["id"]), "write_jobs_created": 0}


@router.post("/hosts/{host_id}/compliance/remediate")
def remediate_host_compliance(
    host_id: UUID, request: Request, body: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """F24 : brouillon de demande de correction — aucun job d'écriture."""
    from .. import compliance
    from .views import change_view

    _only(body, set())
    with unit_of_work(request, write=True) as u:

        def act() -> dict[str, Any]:
            result = compliance.propose_remediation(
                u.conn, u.p, host_id, request_id=u.request_id
            )
            change = u.conn.execute(
                "SELECT * FROM changes WHERE id = %s", (UUID(result["change_id"]),)
            ).fetchone()
            return {**result, "change": change_view(u.conn, u.p, change)}

        return idempotent(u, request, body, act)


@router.post("/hosts/{host_id}/rudder/import-observations")
def import_rudder_observations(
    host_id: UUID, request: Request, body: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """F24 / RAC-36 : importe des observations Rudder (lecture seule).

    Connecteur fictif (`rudder-fake`) : synchrone, fixtures explicites.
    Connecteur réel : met en file un job `rudder_import` (worker + secret).
    """
    from ..compliance import rudder as rudder_compliance

    _only(body, {"report", "connector_id", "node_id"})
    with unit_of_work(request, write=True) as u:
        report = body.get("report")
        if report is not None and not isinstance(report, dict):
            raise ApiError(422, "invalid_report", "report doit être un objet", field="/report")
        connector_raw = body.get("connector_id")
        parsed_connector: UUID | None = None
        if connector_raw is not None:
            try:
                parsed_connector = UUID(str(connector_raw))
            except ValueError as exc:
                raise ApiError(
                    422, "invalid_connector_id", "UUID de connecteur attendu", field="/connector_id"
                ) from exc
        node_id = body.get("node_id")
        if node_id is not None and not isinstance(node_id, str):
            raise ApiError(422, "invalid_node_id", "node_id doit être une chaîne", field="/node_id")
        return rudder_compliance.request_import(
            u.conn,
            u.p,
            host_id,
            connector_id=parsed_connector,
            node_id=node_id,
            report=report,
            request_id=u.request_id,
        )


@router.get("/dependency-graph")
def get_dependency_graph(request: Request) -> dict[str, Any]:
    """F04 : graphe de dépendances filtré par projet (hors périmètre exclus)."""
    from .. import graph

    with unit_of_work(request) as u:
        return graph.get_graph(u.conn, u.p)


@router.post("/dependency-graph/rebuild")
def rebuild_dependency_graph(request: Request) -> dict[str, Any]:
    """F04 : reconstruit l'instantané du graphe (lecture seule, aucune écriture fournisseur)."""
    from .. import graph

    with unit_of_work(request, write=True) as u:
        result = graph.build(u.conn, built_by=f"user:{u.p.user_id}")
        audit.record(
            u.conn,
            actor_type="user",
            actor_id=str(u.p.user_id),
            action="dependency_graph.rebuild",
            outcome="ok",
            targets={"build": result["build_id"]},
            request_id=u.request_id,
        )
        return result


@router.get("/dependency-graph/impact/{kind}/{stable_id}")
def dependency_impact(kind: str, stable_id: str, request: Request) -> dict[str, Any]:
    """F04 : vue d'impact — VM dépendant d'une ressource, sans fuite hors périmètre."""
    from .. import graph

    with unit_of_work(request) as u:
        return graph.impact(u.conn, u.p, kind=kind, stable_id=stable_id)


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: UUID, request: Request) -> Response:
    with unit_of_work(request) as u:
        row = u.conn.execute("SELECT * FROM artifacts WHERE id = %s", (artifact_id,)).fetchone()
        if row is None or row["key_class"] != "B" or not u.p.can_see_project(row["project_id"]):
            raise not_found("artefact")
        content = artifacts.read(row)
        audit.record(
            u.conn,
            actor_type="user",
            actor_id=str(u.p.user_id),
            action="artifact.download",
            outcome="ok",
            project_id=row["project_id"],
            correlation_id=row["change_id"],
            targets={"artifact": str(artifact_id), "kind": row["kind"]},
            request_id=u.request_id,
        )
    return Response(
        content,
        media_type="application/json" if row["kind"].endswith("json") else "text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{row["kind"]}-{artifact_id}.txt"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/diagnostic-packs/{pack_id}")
def get_diagnostic_pack(pack_id: UUID, request: Request) -> dict[str, Any]:
    """F22 : métadonnées d'un dossier ; hors périmètre → 404."""
    from .. import diagnostics

    with unit_of_work(request) as u:
        return diagnostics.get_pack(u.conn, u.p, pack_id)


@router.get("/diagnostic-packs/{pack_id}/download")
def download_diagnostic_pack(pack_id: UUID, request: Request) -> Response:
    """F22 : téléchargement de l'export filtré ; droits revérifiés ; hors projet → 404."""
    from .. import diagnostics

    with unit_of_work(request) as u:
        meta, content = diagnostics.download_pack(u.conn, u.p, pack_id, request_id=u.request_id)
    return Response(
        content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="diagnostic-{meta["id"]}.json"',
            "Cache-Control": "no-store",
            "X-Rackloom-Digest": meta["digest"],
        },
    )
