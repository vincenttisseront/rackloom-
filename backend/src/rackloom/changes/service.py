"""Changements : besoin versionné, révisions résolues immuables, soumission, modification.

Un changement crée un déploiement (`kind=create`) ou en modifie un (`kind=modify`). Chaque
version du besoin produit une révision (résolue, incomplète ou bloquée) ; la soumission exige
une révision résolue, réserve les noms et met le plan en file. On ne résout jamais à nouveau
en silence : une nouvelle révision est toujours une action explicite.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from .. import audit, events, projects, resolver
from ..canonical import digest
from ..db import Conn, Jsonb
from ..errors import ApiError, not_found
from ..executions import queue
from ..identity.authz import Principal, require_project, require_role
from ..ids import uuid7
from .transitions import EDITABLE, STATES, release_names, transition

SOURCES = ("webui", "chat", "api", "ci", "duplicate")
ACTIVE_STATES = {"validated", "planning", "plan_ready", "approved", "executing", "verifying", "uncertain",
                 "reconciling"}


def change_ref(change: dict[str, Any]) -> str:
    return f"CHG-{change['seq']:04d}"


def get_change(conn: Conn, principal: Principal, change_id: UUID,
               for_update: bool = False) -> dict[str, Any]:
    row = conn.execute(f"SELECT * FROM changes WHERE id = %s{' FOR UPDATE' if for_update else ''}",
                       (change_id,)).fetchone()
    if row is None:
        raise not_found("demande")
    require_project(principal, row["project_id"], "demande")
    return row


def get_deployment(conn: Conn, principal: Principal, deployment_id: UUID,
                   for_update: bool = False) -> dict[str, Any]:
    row = conn.execute(f"SELECT * FROM deployments WHERE id = %s{' FOR UPDATE' if for_update else ''}",
                       (deployment_id,)).fetchone()
    if row is None:
        raise not_found("déploiement")
    require_project(principal, row["project_id"], "déploiement")
    return row


def _project(conn: Conn, project_id: UUID) -> dict[str, Any]:
    return conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()


def _created(conn: Conn, principal: Principal, row: dict[str, Any], request_id: str | None) -> None:
    conn.execute(
        "INSERT INTO state_transitions (change_id, from_state, to_state, reason_code, actor_type, actor_id) "
        "VALUES (%s, NULL, 'draft', 'created', 'user', %s)", (row["id"], str(principal.user_id)))
    events.emit(conn, type="change.created", source="request", project_id=row["project_id"],
                correlation_id=row["id"], data={"ref": change_ref(row), "kind": row["kind"], "title": row["title"]},
                dedupe_key=f"change.created:{row['id']}")
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action=f"change.create.{row['kind']}",
                 outcome="ok", project_id=row["project_id"], correlation_id=row["id"],
                 targets={"ref": change_ref(row), "deployment": str(row["deployment_id"] or "")},
                 request_id=request_id)


def create_change(conn: Conn, principal: Principal, project: dict[str, Any], title: str = "",
                  request_id: str | None = None) -> dict[str, Any]:
    require_role(principal, "operator", project["id"])
    row = conn.execute(
        "INSERT INTO changes (id, kind, project_id, title, state, created_by) "
        "VALUES (%s, 'create', %s, %s, 'draft', %s) RETURNING *",
        (uuid7(), project["id"], (title or "Nouvelle demande").strip()[:120], principal.user_id)).fetchone()
    _created(conn, principal, row, request_id)
    return row


def create_modify_change(conn: Conn, principal: Principal, deployment_id: UUID,
                         request_id: str | None = None) -> dict[str, Any]:
    """« Modifier ce déploiement » : même version de template, entrées modifiables seulement (V10)."""
    dep = get_deployment(conn, principal, deployment_id, for_update=True)
    require_role(principal, "operator", dep["project_id"], dep["environment_id"])
    if dep["status"] != "active":
        raise ApiError(409, "deployment_not_active", "seul un déploiement actif peut être modifié")
    busy = conn.execute("SELECT id FROM changes WHERE deployment_id = %s AND state = ANY(%s)",
                        (deployment_id, list(ACTIVE_STATES | EDITABLE - {"rejected", "plan_failed"}))).fetchone()
    if busy:
        raise ApiError(409, "change_in_progress", "un changement est déjà en cours sur ce déploiement",
                       details={"change_id": str(busy["id"])})
    names = ", ".join(i["name"] for i in dep["instances"])
    row = conn.execute(
        "INSERT INTO changes (id, kind, deployment_id, project_id, environment_id, title, state, created_by) "
        "VALUES (%s, 'modify', %s, %s, %s, %s, 'draft', %s) RETURNING *",
        (uuid7(), deployment_id, dep["project_id"], dep["environment_id"], f"Modifier {names}"[:120],
         principal.user_id)).fetchone()
    _created(conn, principal, row, request_id)
    return row


def add_version(conn: Conn, principal: Principal, change_id: UUID, body: dict[str, Any],
                request_id: str | None = None) -> dict[str, Any]:
    """Enregistre une version du besoin et sa révision. Révoque toute approbation non consommée."""
    if not isinstance(body, dict):
        raise ApiError(422, "field_not_allowed", "objet attendu", field="/")
    for key in body:
        if key not in ("need", "provenance", "base_version_no", "source"):
            if resolver._is_technical(key):
                raise ApiError(422, "locked_field", resolver.LOCKED_MESSAGE, field=f"/{key}")
            raise ApiError(422, "field_not_allowed", f"champ « {key} » refusé : seul le besoin est accepté",
                           field=f"/{key}")
    need = resolver.validate_need(body.get("need"))
    provenance = resolver.validate_provenance(body.get("provenance"), need)
    change = get_change(conn, principal, change_id, for_update=True)
    require_role(principal, "operator", change["project_id"])
    if change["state"] not in EDITABLE:
        raise ApiError(409, "not_editable", f"demande non modifiable dans l'état « {STATES[change['state']]} »")
    base = body.get("base_version_no")
    if not isinstance(base, int) or isinstance(base, bool) or base != change["current_version_no"]:
        raise ApiError(409, "stale_version", "une version plus récente existe : rechargez la demande",
                       details={"current_version_no": change["current_version_no"]})
    source = body.get("source", "api")
    if source not in SOURCES:
        raise ApiError(422, "invalid_source", f"source attendue : {', '.join(SOURCES)}", field="/source")
    blocking = resolver.unconfirmed(provenance, ("/service/template", "/environment"))
    if blocking:
        raise ApiError(422, "unconfirmed_suggestion", "confirmez d'abord le service et l'environnement proposés",
                       field=blocking[0])
    project = _project(conn, change["project_id"])
    deployment = conn.execute("SELECT * FROM deployments WHERE id = %s", (change["deployment_id"],)).fetchone() \
        if change["deployment_id"] else None
    resolution = resolver.resolve(
        conn,
        principal=principal,
        project=project,
        need=need,
        provenance=provenance,
        change_id=change_id,
        deployment=deployment,
        change_kind=change["kind"],
    )
    version_no = change["current_version_no"] + 1
    revision_no = change["current_revision_no"] + 1
    conn.execute(
        "INSERT INTO request_versions (id, change_id, version_no, need, provenance, need_sha256, source, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (uuid7(), change_id, version_no, Jsonb(need), Jsonb(provenance), "sha256:" + digest(need), source,
         principal.user_id))
    full_provenance = {p: {"source": v["source"]} for p, v in resolution["values"].items()}
    full_provenance.update({f"/inputs/{k}": {"source": s} for k, s in resolution.get("input_sources", {}).items()})
    full_provenance.update(provenance)
    env = resolution.get("environment")
    tpl = resolution.get("template")
    revision = conn.execute(
        """
        INSERT INTO request_revisions (id, change_id, revision_no, version_no, outcome, template_version_id,
            environment_id, environment_policy_version, policy_bundle_version, inventory_snapshot_id,
            inventory_observed_at, resolved, provenance, block_reasons, resolution_digest)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (uuid7(), change_id, revision_no, version_no, resolution["outcome"],
         UUID(tpl["template_version_id"]) if tpl else None, UUID(env["id"]) if env else None,
         env["policy_version"] if env else None, resolution["policy_bundle"]["version"],
         resolution["inventory"]["snapshot_id"], resolution["inventory"]["observed_at"], Jsonb(resolution),
         Jsonb(full_provenance), Jsonb(resolution["blocks"]), resolution["resolution_digest"])).fetchone()
    change = conn.execute(
        "UPDATE changes SET current_version_no = %s, current_revision_no = %s, "
        "environment_id = coalesce(%s, environment_id), updated_at = now() WHERE id = %s RETURNING *",
        (version_no, revision_no, UUID(env["id"]) if env else None, change_id)).fetchone()
    _supersede(conn, change, version_no)
    release_names(conn, change)
    target = {"resolved": "draft", "incomplete": "needs_info", "blocked": "resolution_blocked"}[resolution["outcome"]]
    change = transition(conn, change, target, reason_code="new_version", actor_type="user",
                        actor_id=str(principal.user_id),
                        detail={"version_no": version_no, "revision_no": revision_no,
                                "outcome": resolution["outcome"],
                                "blocks": [b["code"] for b in resolution["blocks"]]},
                        request_id=request_id)
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action="change.version.create",
                 outcome=resolution["outcome"], project_id=change["project_id"], correlation_id=change_id,
                 versions={"version_no": version_no, "revision_no": revision_no,
                           "template": tpl["version"] if tpl else None,
                           "environment_policy": env["policy_version"] if env else None,
                           "policy_bundle": resolution["policy_bundle"]["version"],
                           "resolution_digest": resolution["resolution_digest"]},
                 targets={"source": source}, request_id=request_id)
    return {"change": change, "version_no": version_no, "revision_no": revision_no, "revision": revision,
            "summary": resolver.summary(resolution, project),
            "unconfirmed_suggestions": resolver.unconfirmed(provenance)}


def _supersede(conn: Conn, change: dict[str, Any], version_no: int) -> None:
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
        """, (change["id"],)).fetchall()
    if plans or revoked:
        events.emit(conn, type="change.superseded", source="system", project_id=change["project_id"],
                    correlation_id=change["id"], severity="warning",
                    data={"version_no": version_no, "plans_superseded": len(plans),
                          "approvals_revoked": len(revoked),
                          "message": "Nouvelle version : plan remplacé, approbation révoquée"})


def current_version(conn: Conn, change: dict[str, Any]) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM request_versions WHERE change_id = %s AND version_no = %s",
                        (change["id"], change["current_version_no"])).fetchone()


def current_revision(conn: Conn, change: dict[str, Any]) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM request_revisions WHERE change_id = %s AND revision_no = %s",
                        (change["id"], change["current_revision_no"])).fetchone()


def get_revision(conn: Conn, change: dict[str, Any], revision_no: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM request_revisions WHERE change_id = %s AND revision_no = %s",
                       (change["id"], revision_no)).fetchone()
    if row is None:
        raise not_found("révision")
    return row


def _reserve(conn: Conn, change: dict[str, Any], revision: dict[str, Any]) -> None:
    env_id = revision["environment_id"]
    for inst in revision["resolved"]["instances"]:
        if inst["existing"]:
            continue
        try:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO name_reservations (id, project_id, environment_id, name, change_id, revision_no) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (uuid7(), change["project_id"], env_id, inst["name"], change["id"], revision["revision_no"]))
        except psycopg.errors.UniqueViolation as exc:
            raise ApiError(409, "name_unavailable", f"le nom {inst['name']} vient d'être réservé par une autre "
                           "demande : enregistrez une nouvelle version") from exc


def _ensure_deployment(conn: Conn, change: dict[str, Any], revision: dict[str, Any],
                       principal: Principal) -> dict[str, Any]:
    if change["deployment_id"]:
        return conn.execute("SELECT * FROM deployments WHERE id = %s", (change["deployment_id"],)).fetchone()
    deployment_id = uuid7()
    stack = conn.execute(
        "INSERT INTO stacks (id, slug, project_id, environment_id, backend_kind, backend_ref) "
        "VALUES (%s, %s, %s, %s, 'local_demo', %s) RETURNING *",
        (uuid7(), str(deployment_id), change["project_id"], revision["environment_id"],
         f"local://{deployment_id}")).fetchone()
    dep = conn.execute(
        "INSERT INTO deployments (id, project_id, environment_id, template_slug, template_version_id, stack_id, "
        "created_by_change_id, owner_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (deployment_id, change["project_id"], revision["environment_id"],
         revision["resolved"]["template"]["slug"], revision["template_version_id"], stack["id"], change["id"],
         change["created_by"])).fetchone()
    conn.execute("UPDATE changes SET deployment_id = %s WHERE id = %s", (deployment_id, change["id"]))
    change["deployment_id"] = deployment_id
    return dep


def submit(conn: Conn, principal: Principal, change_id: UUID, revision_no: int,
           request_id: str | None = None) -> dict[str, Any]:
    change = get_change(conn, principal, change_id, for_update=True)
    require_role(principal, "operator", change["project_id"], change["environment_id"])
    if revision_no != change["current_revision_no"]:
        raise ApiError(409, "stale_revision", "la révision soumise n'est plus la révision courante")
    revision = current_revision(conn, change)
    if revision is None:
        raise ApiError(409, "no_revision", "aucune révision à soumettre")
    if revision["outcome"] != "resolved":
        first = (revision["block_reasons"] or revision["resolved"].get("missing") or [{}])[0]
        raise ApiError(422, "not_resolved", first.get("message") or first.get("text") or "besoin incomplet",
                       field=first.get("field"))
    version = current_version(conn, change)
    pending = resolver.unconfirmed(version["provenance"])
    if pending:
        raise ApiError(422, "unconfirmed_suggestion", "des valeurs proposées par l'assistant ne sont pas confirmées",
                       field=pending[0])
    if change["state"] not in ("draft", "plan_expired"):
        raise ApiError(409, "invalid_state", f"soumission impossible dans l'état « {STATES[change['state']]} »")
    _reserve(conn, change, revision)
    dep = _ensure_deployment(conn, change, revision, principal)
    if change["state"] == "draft":
        change = transition(conn, dict(change), "validated", reason_code="submitted", actor_type="user",
                            actor_id=str(principal.user_id), detail={"revision_no": revision_no},
                            request_id=request_id)
    # F40 / F58 : restauration ou redémarrage — plan simulé prêt à approuver (pas de Terraform).
    if change["kind"] in ("restore", "restart"):
        from datetime import UTC, datetime, timedelta

        from .. import artifacts
        from ..canonical import digest as canon_digest
        from ..executions import bundle as bundles

        if change["kind"] == "restore":
            from .. import restore as restore_mod

            restore_mod.verify_plan(revision["resolved"], revision["resolved"]["restore"]["source_instance"])
            reason_plan, reason_ready = "restore_plan", "restore_plan_ready"
            plan_bytes = b'{"rackloom":"restore-plan","simulated":true}'
            summary = {
                "simulated": True,
                "restore": True,
                "destructive": False,
                "no_changes": False,
                "terraform_version": None,
                "vm_counts": {"create": 1, "update": 0, "delete": 0},
                "message": "Restauration en réseau isolé (simulée)",
                "interruption": False,
            }
        else:
            reason_plan, reason_ready = "restart_plan", "restart_plan_ready"
            plan_bytes = b'{"rackloom":"restart-plan","simulated":true,"impact":"interruption"}'
            names = [i["name"] for i in (revision["resolved"].get("instances") or [])]
            summary = {
                "simulated": True,
                "restart": True,
                "destructive": False,
                "no_changes": True,
                "terraform_version": None,
                "vm_counts": {"create": 0, "update": 0, "delete": 0, "replace": 0},
                "message": "Redémarrage demandé (simulé) — interruption de service",
                "interruption": True,
                "hosts": names,
            }
        change = transition(conn, change, "planning", reason_code=reason_plan, actor_type="user",
                            actor_id=str(principal.user_id), detail={"revision_no": revision_no},
                            request_id=request_id)
        art = artifacts.store(
            conn,
            project_id=change["project_id"],
            change_id=change["id"],
            kind="tf_plan_bin",
            key_class="A",
            plaintext=plan_bytes,
        )
        env = conn.execute("SELECT * FROM environments WHERE id = %s", (change["environment_id"],)).fetchone()
        valid_until = datetime.now(UTC) + timedelta(seconds=env["plan_ttl_seconds"])
        version = current_version(conn, change)
        stack = conn.execute("SELECT * FROM stacks WHERE id = %s", (dep["stack_id"],)).fetchone()
        bundle = bundles.build(
            conn,
            change=change,
            version=version,
            revision=revision,
            stack=stack,
            plan_sha256=art["sha256"],
            state_lineage=None,
            state_serial=None,
            valid_until=valid_until.isoformat(),
            terraform_version=None,
        )
        plan_id = uuid7()
        conn.execute(
            """
            INSERT INTO plans (id, change_id, version_no, revision_no, stack_id, job_id, status, plan_sha256,
                               artifact_id, summary, counts, valid_until, bundle, bundle_digest, state_lineage,
                               state_serial)
            VALUES (%s, %s, %s, %s, %s, NULL, 'ready', %s, %s, %s, %s, %s, %s, %s, NULL, NULL)
            """,
            (
                plan_id,
                change["id"],
                revision["version_no"],
                revision["revision_no"],
                dep["stack_id"],
                art["sha256"],
                art["id"],
                Jsonb(summary),
                Jsonb(summary["vm_counts"]),
                valid_until,
                Jsonb(bundle),
                canon_digest(bundle),
            ),
        )
        change = transition(
            conn,
            change,
            "plan_ready",
            reason_code=reason_ready,
            actor_type="user",
            actor_id=str(principal.user_id),
            detail={"plan_id": str(plan_id), "simulated": True},
            request_id=request_id,
        )
        return {"change": change, "job_id": None, "deployment_id": dep["id"], "plan_id": plan_id}

    change = transition(conn, change, "planning", reason_code="plan_requested", actor_type="user",
                        actor_id=str(principal.user_id), detail={"revision_no": revision_no}, request_id=request_id)
    # F31 : préflight (lecture seule) avant tf_plan ; tf_plan n'est enfilé qu'après succès.
    job = queue.enqueue(conn, kind="preflight", change=change, stack_id=dep["stack_id"],
                        idempotency_key=f"preflight:{change_id}:{revision_no}:{change['row_version']}",
                        payload={"revision_no": revision_no, "version_no": revision["version_no"]})
    return {"change": change, "job_id": job["id"], "deployment_id": dep["id"]}


def duplicate(conn: Conn, principal: Principal, change_id: UUID,
              request_id: str | None = None) -> dict[str, Any]:
    """Nouveau changement avec le même besoin : nouvelle résolution avec les versions courantes."""
    original = get_change(conn, principal, change_id)
    if original["kind"] != "create":
        raise ApiError(409, "not_duplicable", "seule une demande de création peut être dupliquée")
    version = current_version(conn, original)
    if version is None:
        raise ApiError(409, "no_version", "aucun besoin à dupliquer")
    project = _project(conn, original["project_id"])
    new = create_change(conn, principal, project, f"Copie de {change_ref(original)}", request_id)
    provenance = {p: e for p, e in version["provenance"].items() if p in resolver.POINTERS}
    result = add_version(conn, principal, new["id"], {"need": version["need"], "provenance": provenance,
                                                      "base_version_no": 0, "source": "duplicate"}, request_id)
    return result


def replan(conn: Conn, principal: Principal, change_id: UUID,
           request_id: str | None = None) -> dict[str, Any]:
    """Nouveau plan de la même révision après succès (lecture seule ; « aucun changement » attendu)."""
    change = get_change(conn, principal, change_id, for_update=True)
    require_role(principal, "operator", change["project_id"], change["environment_id"])
    if change["state"] != "succeeded":
        raise ApiError(409, "invalid_state", "nouvelle planification possible uniquement après un succès")
    dep = conn.execute("SELECT * FROM deployments WHERE id = %s", (change["deployment_id"],)).fetchone()
    change = transition(conn, change, "planning", reason_code="replan_requested", actor_type="user",
                        actor_id=str(principal.user_id), request_id=request_id)
    job = queue.enqueue(conn, kind="preflight", change=change, stack_id=dep["stack_id"],
                        idempotency_key=f"preflight:{change_id}:{change['current_revision_no']}:{change['row_version']}",
                        payload={"revision_no": change["current_revision_no"],
                                 "version_no": change["current_version_no"], "replan": True})
    return {"change": change, "job_id": job["id"]}


def cancel_job(conn: Conn, principal: Principal, job_id: UUID,
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
    events.emit(conn, type="job.cancel_requested", source="system", project_id=job["project_id"],
                correlation_id=job["change_id"], job_id=job_id, severity="warning",
                data={"kind": job["kind"], "message": "Annulation demandée : non garantie si le job a démarré"},
                dedupe_key=f"job.cancel_requested:{job_id}")
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action="job.cancel_request",
                 outcome="requested", project_id=job["project_id"], correlation_id=job["change_id"],
                 targets={"job": str(job_id), "kind": job["kind"]}, request_id=request_id)
    return {"job_id": str(job_id), "state": job["state"], "cancel_requested_at": job["cancel_requested_at"]}


def cancel(conn: Conn, principal: Principal, change_id: UUID,
           request_id: str | None = None) -> dict[str, Any]:
    change = get_change(conn, principal, change_id, for_update=True)
    if not (principal.has_role("operator", change["project_id"])
            or principal.has_role("approver", change["project_id"])):
        raise ApiError(403, "forbidden", "rôle opérateur ou approbateur requis")
    if change["state"] in ("executing", "verifying", "uncertain", "reconciling"):
        raise ApiError(409, "use_job_cancel", "exécution en cours : demandez l'annulation du job en cours")
    conn.execute("UPDATE jobs SET state = 'cancelled', finished_at = now(), updated_at = now() "
                 "WHERE change_id = %s AND state = 'queued'", (change_id,))
    return transition(conn, change, "cancelled", reason_code="cancelled_by_user", actor_type="user",
                      actor_id=str(principal.user_id), request_id=request_id)


def referent(conn: Conn, project_id: UUID) -> str:
    return projects.referent_name(conn, _project(conn, project_id))
