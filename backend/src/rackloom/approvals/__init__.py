"""Approbation liée à l'empreinte du dossier d'exécution. Seule voie de création d'une approbation.

L'approbation, l'exécution et le job d'écriture sont créés dans la même transaction.
Le contexte d'authentification de l'approbateur est enregistré (base de F12).
"""


from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import audit, clock, events
from ..changes.service import get_change
from ..changes.transitions import transition
from ..db import Conn, Jsonb
from ..errors import ApiError
from ..executions import queue
from ..identity import service_accounts
from ..identity.authz import Principal, require_role
from ..ids import uuid7
from ..projects import calendars, freezes


def _auth_context(principal: Principal) -> dict[str, Any]:
    return {
        "method": principal.auth_method,
        "auth_time": principal.auth_time.isoformat(),
        "session_id": str(principal.session_id),
        "email": principal.email,
        "acr": getattr(principal, "acr", "pwd"),
        "amr": list(getattr(principal, "amr", ["pwd"]) or ["pwd"]),
    }


def decide(conn: Conn, principal: Principal, change_id: UUID, body: dict[str, Any],
           request_id: str | None = None) -> dict[str, Any]:
    from ..identity import reauth

    service_accounts.refuse_approval(principal)
    allowed = {"plan_id", "retry_id", "bundle_digest", "decision", "comment"}
    extra = set(body) - allowed
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé", field=f"/{sorted(extra)[0]}")
    decision = body.get("decision")
    if decision not in ("approved", "rejected"):
        raise ApiError(422, "invalid_decision", "décision attendue : approved ou rejected", field="/decision")
    comment = str(body.get("comment") or "").strip()[:1000]
    if decision == "rejected" and not comment:
        raise ApiError(422, "comment_required", "un motif est obligatoire pour refuser", field="/comment")
    # F12 : lire un plan n'exige rien ; approuver (et refuser de façon critique) exige une preuve fraîche.
    if decision == "approved":
        reauth.require_fresh(principal, action="approuver un plan")
    change = get_change(conn, principal, change_id, for_update=True)
    require_role(principal, "approver", change["project_id"], change["environment_id"])
    if decision == "approved" and freezes.active(conn, change["environment_id"], lock=True):
        raise ApiError(409, "env_frozen", "environnement gelé : approbation suspendue jusqu'au dégel")
    if body.get("retry_id"):
        return _decide_retry(conn, principal, change, UUID(str(body["retry_id"])), body, decision, comment,
                             request_id)
    try:
        plan_id = UUID(str(body.get("plan_id")))
    except ValueError as exc:
        raise ApiError(422, "invalid_plan", "plan_id attendu", field="/plan_id") from exc
    plan = conn.execute("SELECT * FROM plans WHERE id = %s AND change_id = %s FOR UPDATE",
                        (plan_id, change_id)).fetchone()
    if plan is None:
        raise ApiError(404, "not_found", "plan introuvable")
    if conn.execute("SELECT 1 FROM approvals WHERE plan_id = %s AND kind = 'plan'", (plan_id,)).fetchone():
        raise ApiError(409, "already_decided", "ce plan a déjà fait l'objet d'une décision")
    if plan["revision_no"] != change["current_revision_no"]:
        raise ApiError(409, "stale_plan", "le plan ne correspond plus à la révision courante de la demande")
    if plan["status"] == "blocked_destructive":
        raise ApiError(409, "destructive_plan",
                       "le plan supprime ou remplace des ressources : approbation impossible au lot 1")
    if plan["status"] != "ready" or change["state"] != "plan_ready":
        raise ApiError(409, "plan_not_ready", f"plan dans l'état « {plan['status']} »")
    if plan["valid_until"] <= clock.now():
        conn.execute("UPDATE plans SET status = 'expired' WHERE id = %s", (plan_id,))
        transition(conn, change, "plan_expired", reason_code="plan_expired", actor_type="system", actor_id=None)
        raise ApiError(409, "plan_expired", "le plan a expiré : un nouveau plan doit être calculé puis approuvé",
                       commit=True)
    if body.get("bundle_digest") != plan["bundle_digest"]:
        raise ApiError(409, "stale_digest", "l'empreinte affichée ne correspond plus au dossier d'exécution : "
                                            "recharger avant de décider")
    author = conn.execute("SELECT created_by FROM request_versions WHERE change_id = %s AND version_no = %s",
                          (change_id, plan["version_no"])).fetchone()["created_by"]
    env = conn.execute("SELECT * FROM environments WHERE id = %s", (change["environment_id"],)).fetchone()
    if author == principal.user_id:
        raise ApiError(403, "self_approval", "vous ne pouvez pas approuver une version que vous avez rédigée")
    if (env["requires_distinct_approver"] or env["kind"] == "prod") and change["created_by"] == principal.user_id:
        raise ApiError(403, "self_approval", "l'approbateur doit être distinct du demandeur pour cet environnement")

    approval_id = uuid7()
    conn.execute(
        """
        INSERT INTO approvals (id, change_id, revision_no, plan_id, kind, decision, bundle_digest, decided_by, comment,
                               session_id, auth_context)
        VALUES (%s, %s, %s, %s, 'plan', %s, %s, %s, %s, %s, %s)
        """,
        (approval_id, change_id, plan["revision_no"], plan_id, decision, plan["bundle_digest"], principal.user_id,
         comment, principal.session_id, Jsonb(_auth_context(principal))),
    )
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action=f"approval.{decision}",
                 outcome="ok", project_id=change["project_id"], correlation_id=change_id,
                 targets={"plan": str(plan_id), "approval": str(approval_id)},
                 versions={"bundle_digest": plan["bundle_digest"], "version_no": plan["version_no"],
                           "revision_no": plan["revision_no"]},
                 policy_decision={"auth_context": _auth_context(principal)}, request_id=request_id)
    events.emit(
        conn,
        type="approval.decided",
        source="approval",
        project_id=change["project_id"],
        correlation_id=change_id,
        data={
            "decision": decision,
            "by": principal.display_name,
            "plan_id": str(plan_id),
            "comment": (comment or "")[:200] or None,
        },
        severity="warning" if decision == "rejected" else "info",
        dedupe_key=f"approval.decided:{approval_id}",
    )
    if decision == "rejected":
        conn.execute("UPDATE plans SET status = 'rejected' WHERE id = %s", (plan_id,))
        change = transition(conn, change, "rejected", reason_code="rejected_by_approver", actor_type="user",
                            actor_id=str(principal.user_id), detail={"comment": comment}, request_id=request_id)
        return {"approval_id": approval_id, "decision": decision, "change": change}

    execution_id = uuid7()
    conn.execute(
        "INSERT INTO executions (id, change_id, approval_id, plan_id, bundle_digest, state) "
        "VALUES (%s, %s, %s, %s, %s, 'pending')",
        (execution_id, change_id, approval_id, plan_id, plan["bundle_digest"]),
    )
    conn.execute("UPDATE approvals SET execution_id = %s WHERE id = %s", (execution_id, approval_id))
    change = transition(conn, change, "approved", reason_code="approved", actor_type="user",
                        actor_id=str(principal.user_id),
                        detail={"bundle_digest": plan["bundle_digest"][:12]}, request_id=request_id)
    run_after = calendars.schedule_run_after(conn, change["environment_id"])
    # F40 : restauration → vm_restore ; F58 : redémarrage → service_restart ;
    # F06 : retrait → lease_retire ; sinon tf_apply.
    if change["kind"] == "restore":
        from .. import restore as restore_mod

        job = restore_mod.enqueue_restore(conn, change, approval_id=approval_id)
        conn.execute(
            "UPDATE jobs SET execution_id = %s, stack_id = %s, run_after = %s, "
            "payload = payload || %s::jsonb WHERE id = %s",
            (
                execution_id,
                plan["stack_id"],
                run_after,
                Jsonb({"plan_id": str(plan_id)}),
                job["id"],
            ),
        )
        job = conn.execute("SELECT * FROM jobs WHERE id = %s", (job["id"],)).fetchone()
    elif change["kind"] == "restart":
        from ..deployments import actions as service_actions

        job = service_actions.enqueue_restart(conn, change, approval_id=approval_id)
        conn.execute(
            "UPDATE jobs SET execution_id = %s, stack_id = %s, run_after = %s, "
            "payload = payload || %s::jsonb WHERE id = %s",
            (
                execution_id,
                plan["stack_id"],
                run_after,
                Jsonb({"plan_id": str(plan_id)}),
                job["id"],
            ),
        )
        job = conn.execute("SELECT * FROM jobs WHERE id = %s", (job["id"],)).fetchone()
    elif change["kind"] == "retire":
        from .. import leases

        job = leases.enqueue_retire(conn, change, approval_id=approval_id)
        conn.execute(
            "UPDATE jobs SET execution_id = %s, stack_id = %s, run_after = %s, "
            "payload = payload || %s::jsonb WHERE id = %s",
            (
                execution_id,
                plan["stack_id"],
                run_after,
                Jsonb({"plan_id": str(plan_id)}),
                job["id"],
            ),
        )
        job = conn.execute("SELECT * FROM jobs WHERE id = %s", (job["id"],)).fetchone()
    elif change["kind"] == "patch":
        from .. import patching

        job = patching.enqueue_patch(conn, change, approval_id=approval_id)
        conn.execute(
            "UPDATE jobs SET execution_id = %s, stack_id = %s, run_after = %s, "
            "payload = payload || %s::jsonb WHERE id = %s",
            (
                execution_id,
                plan["stack_id"],
                run_after,
                Jsonb({"plan_id": str(plan_id)}),
                job["id"],
            ),
        )
        job = conn.execute("SELECT * FROM jobs WHERE id = %s", (job["id"],)).fetchone()
    else:
        job = queue.enqueue(
            conn,
            kind="tf_apply",
            change=change,
            stack_id=plan["stack_id"],
            execution_id=execution_id,
            idempotency_key=f"tf_apply:{execution_id}",
            payload={"plan_id": str(plan_id)},
            run_after=run_after,
        )
    return {"approval_id": approval_id, "decision": decision, "execution_id": execution_id, "job_id": job["id"],
            "change": change, "run_after": run_after}


def request_retry(
    conn: Conn, principal: Principal, change_id: UUID, body: dict[str, Any], request_id: str | None = None
) -> dict[str, Any]:
    """Reprise ciblée de la configuration après un succès partiel ; à approuver ensuite."""
    from ..canonical import digest

    change = get_change(conn, principal, change_id, for_update=True)
    require_role(principal, "operator", change["project_id"], change["environment_id"])
    if change["state"] != "partially_succeeded":
        raise ApiError(409, "invalid_state", "reprise possible uniquement après un succès partiel")
    if body.get("step") != "ansible_configure":
        raise ApiError(422, "invalid_step", "seule la reprise de la configuration est prise en charge", field="/step")
    execution = conn.execute(
        "SELECT * FROM executions WHERE change_id = %s ORDER BY created_at DESC LIMIT 1", (change_id,)
    ).fetchone()
    failed = conn.execute(
        "SELECT id, name FROM hosts WHERE deployment_id = %s "
        "AND deployment_state IN ('config_failed', 'enrolled', 'wave_blocked') ORDER BY name",
        (change["deployment_id"],),
    ).fetchall()
    requested = body.get("host_ids") or [str(h["id"]) for h in failed]
    known = {str(h["id"]) for h in failed}
    if not requested or not set(requested) <= known:
        raise ApiError(422, "invalid_hosts", "hôtes de reprise inconnus ou déjà configurés", field="/host_ids")
    plan = conn.execute("SELECT * FROM plans WHERE id = %s", (execution["plan_id"],)).fetchone()
    bundle = {
        "schema": "rackloom.retry-bundle/v1",
        "change_id": str(change_id),
        "execution_id": str(execution["id"]),
        "step": "ansible_configure",
        "host_ids": sorted(requested),
        "revision_no": plan["revision_no"],
        "plan_bundle_digest": plan["bundle_digest"],
        "components": plan["bundle"]["components"],
        "ansible": plan["bundle"]["ansible"],
        "runner_images": plan["bundle"]["runner_images"],
        "policy_bundle": plan["bundle"]["policy_bundle"],
    }
    retry_id = uuid7()
    conn.execute(
        "INSERT INTO retry_requests (id, change_id, execution_id, step, host_ids, bundle, bundle_digest, requested_by) "
        "VALUES (%s, %s, %s, 'ansible_configure', %s, %s, %s, %s)",
        (
            retry_id,
            change_id,
            execution["id"],
            Jsonb(sorted(requested)),
            Jsonb(bundle),
            digest(bundle),
            principal.user_id,
        ),
    )
    events.emit(
        conn,
        type="retry.requested",
        source="system",
        project_id=change["project_id"],
        correlation_id=change_id,
        data={"retry_id": str(retry_id), "hosts": len(requested)},
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="retry.request",
        outcome="ok",
        project_id=change["project_id"],
        correlation_id=change_id,
        targets={"retry": str(retry_id)},
        request_id=request_id,
    )
    return {"retry_id": retry_id, "bundle_digest": digest(bundle), "host_ids": sorted(requested)}


def _decide_retry(
    conn: Conn,
    principal: Principal,
    change: dict[str, Any],
    retry_id: UUID,
    body: dict[str, Any],
    decision: str,
    comment: str,
    request_id: str | None,
) -> dict[str, Any]:
    retry = conn.execute(
        "SELECT * FROM retry_requests WHERE id = %s AND change_id = %s FOR UPDATE", (retry_id, change["id"])
    ).fetchone()
    if retry is None:
        raise ApiError(404, "not_found", "demande de reprise introuvable")
    if retry["status"] != "pending":
        raise ApiError(409, "already_decided", "cette reprise a déjà fait l'objet d'une décision")
    if body.get("bundle_digest") != retry["bundle_digest"]:
        raise ApiError(409, "stale_digest", "l'empreinte de la reprise a changé : recharger")
    if retry["requested_by"] == principal.user_id:
        raise ApiError(403, "self_approval", "vous ne pouvez pas approuver votre propre reprise")
    approval_id = uuid7()
    conn.execute(
        """
        INSERT INTO approvals (id, change_id, revision_no, execution_id, retry_request_id, kind, decision,
                               bundle_digest, decided_by, comment, session_id, auth_context, retry_scope)
        VALUES (%s, %s, %s, %s, %s, 'retry', %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            approval_id,
            change["id"],
            retry["bundle"]["revision_no"],
            retry["execution_id"],
            retry_id,
            decision,
            retry["bundle_digest"],
            principal.user_id,
            comment,
            principal.session_id,
            Jsonb(_auth_context(principal)),
            Jsonb({"step": retry["step"], "host_ids": retry["host_ids"]}),
        ),
    )
    conn.execute("UPDATE retry_requests SET status = %s WHERE id = %s", (decision, retry_id))
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action=f"approval.retry.{decision}",
        outcome="ok",
        project_id=change["project_id"],
        correlation_id=change["id"],
        targets={"retry": str(retry_id), "approval": str(approval_id)},
        versions={"bundle_digest": retry["bundle_digest"]},
        request_id=request_id,
    )
    events.emit(
        conn,
        type="approval.decided",
        source="approval",
        project_id=change["project_id"],
        correlation_id=change["id"],
        data={
            "decision": decision,
            "by": principal.display_name,
            "kind": "retry",
            "retry_id": str(retry_id),
            "comment": (comment or "")[:200] or None,
        },
        severity="warning" if decision == "rejected" else "info",
        dedupe_key=f"approval.decided:{approval_id}",
    )
    if decision == "rejected":
        return {"approval_id": approval_id, "decision": decision, "change": change}
    change = transition(
        conn,
        change,
        "executing",
        reason_code="retry_approved",
        actor_type="user",
        actor_id=str(principal.user_id),
        request_id=request_id,
    )
    execution = conn.execute("SELECT plan_id FROM executions WHERE id = %s", (retry["execution_id"],)).fetchone()
    plan = conn.execute("SELECT bundle FROM plans WHERE id = %s", (execution["plan_id"],)).fetchone()
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM hosts WHERE id = ANY(%s::uuid[])", (list(retry["host_ids"]),)
        ).fetchall()
    }
    targets = plan["bundle"]["targets"]
    waves = [[n for n in w if n in names] for w in targets.get("waves") or [targets["instances"]]]
    run_after = calendars.schedule_run_after(conn, change["environment_id"])
    job = queue.enqueue(
        conn,
        kind="ansible_configure",
        change=change,
        execution_id=retry["execution_id"],
        idempotency_key=f"ansible_configure:retry:{retry_id}",
        payload={
            "host_ids": retry["host_ids"],
            "retry_approval_id": str(approval_id),
            "waves": [w for w in waves if w],
            "wave": 0,
        },
        run_after=run_after,
    )
    return {"approval_id": approval_id, "decision": decision, "job_id": job["id"], "change": change}
