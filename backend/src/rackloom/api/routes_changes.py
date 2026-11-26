"""Demandes : versions du besoin, révisions, soumission, plans, approbations, reprises, annulation.

Chaque action passe d'abord par `workflow.require_action`, qui réévalue `allowed_actions`
au moment de l'appel ; le service applique ensuite ses propres contrôles.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request

from .. import approvals, workflow
from ..changes import service
from ..errors import ApiError, not_found
from .deps import csrf_guard, idempotent, unit_of_work
from .views import change_view, plan_view, revision_view

router = APIRouter(tags=["demandes"], dependencies=[Depends(csrf_guard)])


def _only(body: dict[str, Any], allowed: set[str]) -> None:
    extra = set(body) - allowed
    if extra:
        key = sorted(extra)[0]
        raise ApiError(422, "unknown_field", f"champ « {key} » refusé", field=f"/{key}")


@router.get("/changes/{change_id}")
def get_change(change_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        return change_view(u.conn, u.p, service.get_change(u.conn, u.p, change_id))


@router.get("/changes/{change_id}/history")
def history(change_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        change = service.get_change(u.conn, u.p, change_id)
        transitions = u.conn.execute(
            "SELECT from_state, to_state, reason_code, actor_type, at, detail FROM state_transitions "
            "WHERE change_id = %s ORDER BY id", (change_id,)).fetchall()
        versions = u.conn.execute(
            "SELECT v.version_no, v.source, v.created_at, u.display_name FROM request_versions v "
            "JOIN users u ON u.id = v.created_by WHERE v.change_id = %s ORDER BY v.version_no",
            (change_id,)).fetchall()
        revisions = u.conn.execute(
            "SELECT revision_no, version_no, outcome, resolution_digest, created_at FROM request_revisions "
            "WHERE change_id = %s ORDER BY revision_no", (change_id,)).fetchall()
        return {"change_id": str(change["id"]), "transitions": transitions, "versions": versions,
                "revisions": revisions}


@router.post("/changes/{change_id}/versions")
def add_version(change_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        change = service.get_change(u.conn, u.p, change_id)

        def act() -> dict[str, Any]:
            workflow.require_action(u.conn, u.p, "change", change, "edit")
            result = service.add_version(u.conn, u.p, change_id, body, u.request_id)
            fresh = service.get_change(u.conn, u.p, change_id)
            return {"version_no": result["version_no"], "revision_no": result["revision_no"],
                    "change": change_view(u.conn, u.p, fresh)}

        return idempotent(u, request, body, act)


@router.get("/changes/{change_id}/revisions/{revision_no}")
def get_revision(change_id: UUID, revision_no: int, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        change = service.get_change(u.conn, u.p, change_id)
        revision = service.get_revision(u.conn, change, revision_no)
        project = u.conn.execute("SELECT * FROM projects WHERE id = %s", (change["project_id"],)).fetchone()
        return revision_view(u.conn, revision, project)


@router.post("/changes/{change_id}/submit")
def submit(change_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _only(body, {"revision_no"})
    with unit_of_work(request, write=True) as u:
        change = service.get_change(u.conn, u.p, change_id)

        def act() -> dict[str, Any]:
            workflow.require_action(u.conn, u.p, "change", change, "submit")
            revision_no = body.get("revision_no")
            if not isinstance(revision_no, int) or isinstance(revision_no, bool):
                raise ApiError(422, "invalid_revision", "numéro de révision attendu", field="/revision_no")
            result = service.submit(u.conn, u.p, change_id, revision_no, u.request_id)
            return {"job_id": str(result["job_id"]), "deployment_id": str(result["deployment_id"]),
                    "change": change_view(u.conn, u.p, result["change"])}

        return idempotent(u, request, body, act)


@router.post("/changes/{change_id}/duplicate")
def duplicate(change_id: UUID, request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _only(body, set())
    with unit_of_work(request, write=True) as u:
        change = service.get_change(u.conn, u.p, change_id)

        def act() -> dict[str, Any]:
            workflow.require_action(u.conn, u.p, "change", change, "duplicate")
            result = service.duplicate(u.conn, u.p, change_id, u.request_id)
            return {"change": change_view(u.conn, u.p, result["change"])}

        return idempotent(u, request, body, act)


@router.post("/changes/{change_id}/replan")
def replan(change_id: UUID, request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _only(body, set())
    with unit_of_work(request, write=True) as u:
        change = service.get_change(u.conn, u.p, change_id)

        def act() -> dict[str, Any]:
            workflow.require_action(u.conn, u.p, "change", change, "replan")
            result = service.replan(u.conn, u.p, change_id, u.request_id)
            return {"job_id": str(result["job_id"]), "change": change_view(u.conn, u.p, result["change"])}

        return idempotent(u, request, body, act)


@router.post("/changes/{change_id}/cancel")
def cancel(change_id: UUID, request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _only(body, set())
    with unit_of_work(request, write=True) as u:
        change = service.get_change(u.conn, u.p, change_id)
        workflow.require_action(u.conn, u.p, "change", change, "cancel")
        return {"change": change_view(u.conn, u.p, service.cancel(u.conn, u.p, change_id, u.request_id))}


@router.get("/changes/{change_id}/plans/{plan_id}")
def get_plan(change_id: UUID, plan_id: UUID, request: Request) -> dict[str, Any]:
    with unit_of_work(request) as u:
        change = service.get_change(u.conn, u.p, change_id)
        plan = u.conn.execute("SELECT * FROM plans WHERE id = %s AND change_id = %s",
                              (plan_id, change["id"])).fetchone()
        if plan is None:
            raise not_found("plan")
        revision = service.get_revision(u.conn, change, plan["revision_no"])
        return plan_view(u.conn, plan, revision)


@router.post("/changes/{change_id}/approvals")
def decide(change_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        change = service.get_change(u.conn, u.p, change_id)
        approve = body.get("decision") == "approved"
        if body.get("retry_id"):
            action = "approve_retry" if approve else "reject_retry"
        else:
            action = "approve" if approve else "reject"

        def act() -> dict[str, Any]:
            workflow.require_action(u.conn, u.p, "change", change, action)
            result = approvals.decide(u.conn, u.p, change_id, body, u.request_id)
            fresh = service.get_change(u.conn, u.p, change_id)
            return {"approval_id": str(result["approval_id"]), "decision": result["decision"],
                    "change": change_view(u.conn, u.p, fresh)}

        return idempotent(u, request, body, act)


@router.post("/changes/{change_id}/retries")
def request_retry(change_id: UUID, request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        change = service.get_change(u.conn, u.p, change_id)

        def act() -> dict[str, Any]:
            workflow.require_action(u.conn, u.p, "change", change, "request_retry")
            result = approvals.request_retry(u.conn, u.p, change_id, body, u.request_id)
            fresh = service.get_change(u.conn, u.p, change_id)
            return {"retry_id": str(result["retry_id"]), "change": change_view(u.conn, u.p, fresh)}

        return idempotent(u, request, body, act)


@router.get("/changes/{change_id}/estimate")
def get_estimate(change_id: UUID, request: Request) -> dict[str, Any]:
    """F56 : estimation de mise à disposition (sans modèle)."""
    from .. import estimates

    with unit_of_work(request) as u:
        change = service.get_change(u.conn, u.p, change_id)
        return estimates.estimate(u.conn, u.p, change)


@router.get("/changes/{change_id}/reviews")
def list_reviews(change_id: UUID, request: Request) -> dict[str, Any]:
    """F54 : sollicitations et écarts depuis la dernière revue."""
    from .. import reviews

    with unit_of_work(request) as u:
        change = service.get_change(u.conn, u.p, change_id)
        return reviews.list_reviews(u.conn, u.p, change)


@router.get("/changes/{change_id}/review-candidates")
def review_candidates(change_id: UUID, request: Request) -> list[dict[str, Any]]:
    """F54 : collègues déjà habilités sollicitables."""
    from .. import reviews

    with unit_of_work(request) as u:
        change = service.get_change(u.conn, u.p, change_id)
        return reviews.candidates(u.conn, u.p, change)


@router.post("/changes/{change_id}/reviews")
def create_review(change_id: UUID, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """F54 : solliciter un collègue habilité (aucun droit créé)."""
    from .. import reviews

    _only(body, {"solicited_user_id", "question", "version_no", "revision_no", "plan_id", "focus"})
    with unit_of_work(request, write=True) as u:
        change = service.get_change(u.conn, u.p, change_id)

        def act() -> dict[str, Any]:
            return reviews.solicit(u.conn, u.p, change, body, u.request_id)

        return idempotent(u, request, body, act)


@router.post("/changes/{change_id}/reviews/{review_id}/replies")
def reply_review(
    change_id: UUID, review_id: UUID, request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """F54 : répondre sur la version soumise (ne modifie pas le plan)."""
    from .. import reviews

    _only(body, {"body"})
    with unit_of_work(request, write=True) as u:
        change = service.get_change(u.conn, u.p, change_id)

        def act() -> dict[str, Any]:
            return reviews.reply(u.conn, u.p, change, review_id, body, u.request_id)

        return idempotent(u, request, body, act)


@router.post("/deployments/{deployment_id}/changes")
def modify_deployment(deployment_id: UUID, request: Request,
                      body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _only(body, set())
    with unit_of_work(request, write=True) as u:
        dep = service.get_deployment(u.conn, u.p, deployment_id)

        def act() -> dict[str, Any]:
            workflow.require_action(u.conn, u.p, "deployment", dep, "modify")
            change = service.create_modify_change(u.conn, u.p, deployment_id, u.request_id)
            return {"change": change_view(u.conn, u.p, change)}

        return idempotent(u, request, body, act)
