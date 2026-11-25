"""Demandes versionnées : création, révisions immuables (besoin + résolution), soumission."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from .. import audit, events, resolver
from ..canonical import digest
from ..db import Jsonb
from ..errors import ApiError, not_found
from ..executions import queue
from ..identity.authz import Principal, require_project, require_role
from ..ids import uuid7
from .transitions import EDITABLE, STATES, transition


def change_ref(change: dict[str, Any]) -> str:
    return f"CHG-{change['seq']:04d}"


def get_change(conn: psycopg.Connection, principal: Principal, change_id: UUID,
               for_update: bool = False) -> dict[str, Any]:
    row = conn.execute(f"SELECT * FROM changes WHERE id = %s{' FOR UPDATE' if for_update else ''}",
                       (change_id,)).fetchone()
    if row is None:
        raise not_found("demande")
    require_project(principal, row["project_id"], "demande")
    return row


def create_change(conn: psycopg.Connection, principal: Principal, project: dict[str, Any], title: str,
                  request_id: str | None = None) -> dict[str, Any]:
    require_role(principal, "operator", project["id"])
    change_id = uuid7()
    row = conn.execute(
        "INSERT INTO changes (id, project_id, title, state, created_by) VALUES (%s, %s, %s, 'draft', %s) "
        "RETURNING *",
        (change_id, project["id"], title.strip()[:120], principal.user_id),
    ).fetchone()
    conn.execute(
        "INSERT INTO state_transitions (change_id, from_state, to_state, reason_code, actor_type, actor_id) "
        "VALUES (%s, NULL, 'draft', 'created', 'user', %s)",
        (change_id, str(principal.user_id)),
    )
    events.emit(conn, type="change.created", source="rackloom", project_id=project["id"],
                correlation_id=change_id, data={"ref": change_ref(row), "title": row["title"]},
                dedupe_key=f"change.created:{change_id}")
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action="change.create", outcome="ok",
                 project_id=project["id"], correlation_id=change_id, targets={"ref": change_ref(row)},
                 request_id=request_id)
    return row


def add_version(conn: psycopg.Connection, principal: Principal, change_id: UUID, body: dict[str, Any],
                request_id: str | None = None) -> dict[str, Any]:
    """Nouvelle révision : besoin validé puis résolu côté serveur. Révoque toute approbation."""
    change = get_change(conn, principal, change_id, for_update=True)
    require_role(principal, "operator", change["project_id"])
    if change["state"] not in EDITABLE:
        raise ApiError(409, "not_editable", f"demande non modifiable dans l'état « {STATES[change['state']]} »")
    base = body.get("base_version_no")
    if not isinstance(base, int) or base != change["current_version_no"]:
        raise ApiError(409, "stale_version", "une version plus récente existe : recharger la demande",
                       details={"current_version_no": change["current_version_no"]})
    source = body.get("source", "form")
    if source not in ("form", "chat", "api"):
        raise ApiError(422, "invalid_source", "source attendue : form, chat ou api", field="/source")
    extra = set(body) - {"need", "provenance", "base_version_no", "source"}
    if extra:
        key = sorted(extra)[0]
        raise ApiError(422, "locked_field" if key in resolver.LOCKED_FIELDS else "unknown_field",
                       f"« {key} » ne peut pas être fourni ici ; seul le besoin est modifiable.", field=f"/{key}")
    need = resolver.parse_need(body.get("need"))
    provenance = resolver.parse_provenance(body.get("provenance"), need)
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (change["project_id"],)).fetchone()
    if need.environment_id is not None:
        require_role(principal, "operator", project["id"], need.environment_id)
    resolution = resolver.resolve(conn, project=project, need=need, provenance=provenance, change_id=change_id)
    content = need.as_content()
    suggestions = resolver.unconfirmed_suggestions(provenance)
    validation = {
        "status": resolution["status"],
        "blocked": resolution["blocked"],
        "questions": resolution["questions"],
        "unconfirmed_suggestions": suggestions,
        "submittable": resolution["status"] == "resolved" and not suggestions,
    }
    version_no = change["current_version_no"] + 1
    conn.execute(
        """
        INSERT INTO request_versions (id, change_id, version_no, content, provenance, content_sha256, resolution,
                                      resolution_sha256, source, created_by, validation)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (uuid7(), change_id, version_no, Jsonb(content), Jsonb(provenance), digest(content), Jsonb(resolution),
         resolver.resolution_digest(resolution), source, principal.user_id, Jsonb(validation)),
    )
    env_id = UUID(resolution["environment"]["id"]) if resolution.get("environment") else None
    change = conn.execute(
        "UPDATE changes SET current_version_no = %s, environment_id = %s, updated_at = now() "
        "WHERE id = %s RETURNING *",
        (version_no, env_id, change_id),
    ).fetchone()
    _supersede(conn, change, principal, version_no)
    target = "draft" if resolution["status"] == "resolved" else "needs_info"
    change = transition(conn, change, target, reason_code="new_version", actor_type="user",
                        actor_id=str(principal.user_id),
                        detail={"version_no": version_no, "resolution": resolution["status"]},
                        request_id=request_id)
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action="change.version.create",
                 outcome=resolution["status"], project_id=change["project_id"], correlation_id=change_id,
                 versions={"version_no": version_no,
                           "template": (resolution.get("template") or {}).get("version"),
                           "policy": resolution["policy"]["version"]},
                 targets={"source": source}, request_id=request_id)
    return {"change": change, "version_no": version_no, "resolution": resolution, "validation": validation}


def _supersede(conn: psycopg.Connection, change: dict[str, Any], principal: Principal, version_no: int) -> None:
    """Une nouvelle version rend caducs les plans prêts et révoque les approbations non consommées."""
    plans = conn.execute(
        "UPDATE plans SET status = 'superseded' WHERE change_id = %s AND status IN ('ready', 'blocked_destructive') "
        "RETURNING id", (change["id"],)).fetchall()
    revoked = conn.execute(
        """
        UPDATE approvals a SET revoked_at = now(), revoked_reason = 'new_version'
        WHERE a.change_id = %s AND a.revoked_at IS NULL AND a.decision = 'approved'
          AND NOT EXISTS (SELECT 1 FROM executions e WHERE e.approval_id = a.id AND e.started_at IS NOT NULL)
        RETURNING id
        """,
        (change["id"],),
    ).fetchall()
    if plans or revoked:
        events.emit(conn, type="change.superseded", source="rackloom", project_id=change["project_id"],
                    correlation_id=change["id"], severity="warning",
                    data={"version_no": version_no, "plans_superseded": len(plans),
                          "approvals_revoked": len(revoked)})


def submit(conn: psycopg.Connection, principal: Principal, change_id: UUID, version_no: int,
           request_id: str | None = None) -> dict[str, Any]:
    change = get_change(conn, principal, change_id, for_update=True)
    require_role(principal, "operator", change["project_id"], change["environment_id"])
    if version_no != change["current_version_no"]:
        raise ApiError(409, "stale_version", "la version soumise n'est plus la version courante")
    version = current_version(conn, change)
    if version is None:
        raise ApiError(409, "no_version", "aucune version à soumettre")
    validation = version["validation"]
    if validation["unconfirmed_suggestions"]:
        raise ApiError(422, "unconfirmed_suggestion",
                       "des valeurs proposées par l'assistant ne sont pas confirmées",
                       field=validation["unconfirmed_suggestions"][0])
    if not validation["submittable"]:
        first = (validation["blocked"] or validation["questions"] or [{}])[0]
        raise ApiError(422, "not_resolved", first.get("message") or first.get("text") or "demande incomplète",
                       field=first.get("field"), details=validation)
    if change["state"] not in ("draft", "plan_expired", "blocked"):
        raise ApiError(409, "invalid_state", f"soumission impossible dans l'état « {STATES[change['state']]} »")
    stack = ensure_stack(conn, change, version["resolution"])
    if change["state"] == "draft":
        change = transition(conn, change, "validated", reason_code="submitted", actor_type="user",
                            actor_id=str(principal.user_id), request_id=request_id)
    change = transition(conn, change, "planning", reason_code="plan_requested", actor_type="user",
                        actor_id=str(principal.user_id), detail={"version_no": version_no}, request_id=request_id)
    job = queue.enqueue(conn, kind="tf_plan", change=change, stack_id=stack["id"],
                        idempotency_key=f"tf_plan:{change_id}:{version_no}:{change['row_version']}",
                        payload={"version_no": version_no})
    return {"change": change, "job_id": job["id"]}


def ensure_stack(conn: psycopg.Connection, change: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any]:
    stack = conn.execute("SELECT * FROM stacks WHERE change_id = %s", (change["id"],)).fetchone()
    if stack:
        return stack
    slug = f"chg-{change['seq']:04d}-{str(change['id'])[-6:]}"
    return conn.execute(
        """
        INSERT INTO stacks (id, slug, project_id, environment_id, change_id, blueprint_slug, backend_kind, backend_ref)
        VALUES (%s, %s, %s, %s, %s, %s, 'local_demo', %s) RETURNING *
        """,
        (uuid7(), slug, change["project_id"], UUID(resolution["environment"]["id"]), change["id"],
         resolution["template"]["slug"], f"local://{slug}"),
    ).fetchone()


def current_version(conn: psycopg.Connection, change: dict[str, Any]) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM request_versions WHERE change_id = %s AND version_no = %s",
                        (change["id"], change["current_version_no"])).fetchone()


def replan(conn: psycopg.Connection, principal: Principal, change_id: UUID,
           request_id: str | None = None) -> dict[str, Any]:
    """Nouvelle planification après succès : vérifie que l'infrastructure correspond toujours à la demande."""
    change = get_change(conn, principal, change_id, for_update=True)
    require_role(principal, "operator", change["project_id"], change["environment_id"])
    if change["state"] != "succeeded":
        raise ApiError(409, "invalid_state", "nouvelle planification possible uniquement après un succès")
    stack = conn.execute("SELECT * FROM stacks WHERE change_id = %s", (change_id,)).fetchone()
    change = transition(conn, change, "planning", reason_code="replan_requested", actor_type="user",
                        actor_id=str(principal.user_id), request_id=request_id)
    job = queue.enqueue(conn, kind="tf_plan", change=change, stack_id=stack["id"],
                        idempotency_key=f"tf_plan:{change_id}:{change['current_version_no']}:{change['row_version']}",
                        payload={"version_no": change["current_version_no"], "replan": True})
    return {"change": change, "job_id": job["id"]}


def cancel_job(conn: psycopg.Connection, principal: Principal, job_id: UUID,
               request_id: str | None = None) -> dict[str, Any]:
    """Demande d'annulation : jamais garantie une fois le runner démarré."""
    job = conn.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
    if job is None or job["project_id"] is None:
        raise not_found("job")
    require_project(principal, job["project_id"], "job")
    if not (principal.has_role("operator", job["project_id"]) or principal.has_role("approver", job["project_id"])):
        raise ApiError(403, "forbidden", "rôle opérateur ou approbateur requis")
    if job["state"] not in ("queued", "leased", "running"):
        raise ApiError(409, "job_finished", "le job est déjà terminé")
    job = conn.execute(
        "UPDATE jobs SET cancel_requested_at = coalesce(cancel_requested_at, now()), cancel_requested_by = %s, "
        "updated_at = now() WHERE id = %s RETURNING *", (principal.user_id, job_id)).fetchone()
    events.emit(conn, type="job.cancel_requested", source="rackloom", project_id=job["project_id"],
                correlation_id=job["change_id"], job_id=job_id, severity="warning",
                data={"kind": job["kind"], "message": "Annulation demandée : non garantie si le job a démarré"},
                dedupe_key=f"job.cancel_requested:{job_id}")
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action="job.cancel_request",
                 outcome="requested", project_id=job["project_id"], correlation_id=job["change_id"],
                 targets={"job": str(job_id), "kind": job["kind"]}, request_id=request_id)
    return {"job_id": job_id, "state": job["state"], "cancel_requested_at": job["cancel_requested_at"]}


def cancel(conn: psycopg.Connection, principal: Principal, change_id: UUID,
           request_id: str | None = None) -> dict[str, Any]:
    change = get_change(conn, principal, change_id, for_update=True)
    if not (principal.has_role("operator", change["project_id"]) or principal.has_role("approver",
                                                                                       change["project_id"])):
        raise ApiError(403, "forbidden", "rôle opérateur ou approbateur requis")
    if change["state"] in ("executing", "verifying", "uncertain", "reconciling"):
        raise ApiError(409, "use_job_cancel", "exécution en cours : demander l'annulation du job en cours")
    conn.execute("UPDATE jobs SET state = 'cancelled', finished_at = now(), updated_at = now() "
                 "WHERE change_id = %s AND state = 'queued'", (change_id,))
    return transition(conn, change, "cancelled", reason_code="cancelled_by_user", actor_type="user",
                      actor_id=str(principal.user_id), request_id=request_id)
