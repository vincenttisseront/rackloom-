"""F59 : demander l'accès à un service livré.

Le demandeur choisit un niveau prévu par le template. Le groupe IdP est résolu côté
serveur via une correspondance approuvée et versionnée. « Accès accordé » seulement
après confirmation du fournisseur. Relance idempotente. Aucun mot de passe.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from .. import audit, events, registry
from ..changes.service import get_deployment
from ..connectors import idp_fake
from ..db import Conn
from ..errors import ApiError, not_found
from ..identity.authz import Principal, require_project, require_role
from ..ids import uuid7

DEFAULT_LEVELS: list[dict[str, str]] = [
    {"id": "read", "label": "Lecture"},
    {"id": "operate", "label": "Exploitation"},
]

STATES = {
    "pending_approval": "En attente d'approbation",
    "rejected": "Refusée",
    "cancelled": "Annulée",
    "approved": "Approuvée (sync en cours)",
    "syncing": "Synchronisation IdP",
    "granted": "Accès accordé",
    "sync_failed": "Échec de synchronisation",
}


def levels_for_template(contract: dict[str, Any]) -> list[dict[str, str]]:
    raw = contract.get("access_levels")
    if not raw:
        return list(DEFAULT_LEVELS)
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict) and item.get("id"):
            out.append({"id": str(item["id"])[:40], "label": str(item.get("label") or item["id"])[:120]})
    return out or list(DEFAULT_LEVELS)


def publish_mapping(
    conn: Conn,
    principal: Principal,
    *,
    template_slug: str,
    environment_kind: str,
    access_level: str,
    access_label: str,
    idp_group: str,
    note: str = "",
) -> dict[str, Any]:
    """Publie une correspondance (admin plateforme). Supersède la version publiée précédente."""
    if not principal.has_role("platform_admin"):
        raise ApiError(403, "forbidden", "administrateur de plateforme requis")
    group = (idp_group or "").strip()
    if not group or len(group) > 200:
        raise ApiError(422, "invalid_group", "groupe IdP invalide", field="/idp_group")
    # Le demandeur ne doit jamais pouvoir forcer un groupe privilégié via l'API publique.
    prev = conn.execute(
        "SELECT * FROM service_access_mappings WHERE template_slug = %s AND environment_kind = %s "
        "AND access_level = %s AND status = 'published' FOR UPDATE",
        (template_slug, environment_kind, access_level),
    ).fetchone()
    version = (prev["version"] + 1) if prev else 1
    if prev:
        conn.execute(
            "UPDATE service_access_mappings SET status = 'superseded' WHERE id = %s",
            (prev["id"],),
        )
    row = conn.execute(
        """
        INSERT INTO service_access_mappings (
            id, template_slug, environment_kind, access_level, access_label, idp_group,
            version, status, published_at, published_by, note
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'published', now(), %s, %s) RETURNING *
        """,
        (
            uuid7(),
            template_slug,
            environment_kind,
            access_level,
            access_label[:120],
            group,
            version,
            principal.user_id,
            (note or "")[:500],
        ),
    ).fetchone()
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="access.mapping.publish",
        outcome="ok",
        targets={
            "mapping": str(row["id"]),
            "template": template_slug,
            "level": access_level,
            "version": version,
        },
    )
    return _mapping_view(row)


def ensure_demo_mappings(conn: Conn, *, published_by: UUID | None = None) -> None:
    """Correspondances de démo pour ubuntu-web (dev + prod). Isolées, idempotentes."""
    seeds = [
        ("ubuntu-web", "dev", "read", "Lecture", "rackloom.demo.vitrine-dev-read"),
        ("ubuntu-web", "dev", "operate", "Exploitation", "rackloom.demo.vitrine-dev-operate"),
        ("ubuntu-web", "prod", "read", "Lecture", "rackloom.demo.vitrine-prod-read"),
        ("ubuntu-web", "prod", "operate", "Exploitation", "rackloom.demo.vitrine-prod-operate"),
    ]
    for slug, kind, level, label, group in seeds:
        exists = conn.execute(
            "SELECT 1 FROM service_access_mappings WHERE template_slug = %s AND environment_kind = %s "
            "AND access_level = %s AND status = 'published'",
            (slug, kind, level),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            INSERT INTO service_access_mappings (
                id, template_slug, environment_kind, access_level, access_label, idp_group,
                version, status, published_at, published_by, note
            ) VALUES (%s, %s, %s, %s, %s, %s, 1, 'published', now(), %s, %s)
            """,
            (
                uuid7(),
                slug,
                kind,
                level,
                label,
                group,
                published_by,
                "Correspondance de démonstration F59 (faux IdP)",
            ),
        )


def resolve_mapping(conn: Conn, *, template_slug: str, environment_kind: str, access_level: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM service_access_mappings WHERE template_slug = %s AND environment_kind = %s "
        "AND access_level = %s AND status = 'published'",
        (template_slug, environment_kind, access_level),
    ).fetchone()
    if row is None:
        raise ApiError(
            422,
            "no_access_mapping",
            "aucune correspondance approuvée pour ce niveau d'accès",
            field="/access_level",
        )
    return row


def levels_available(conn: Conn, dep: dict[str, Any]) -> list[dict[str, Any]]:
    tpl = registry.template_by_id(conn, dep["template_version_id"])
    env = conn.execute("SELECT kind FROM environments WHERE id = %s", (dep["environment_id"],)).fetchone()
    levels = levels_for_template(tpl["contract"])
    out: list[dict[str, Any]] = []
    for lvl in levels:
        mapping = conn.execute(
            "SELECT id, access_label, version FROM service_access_mappings "
            "WHERE template_slug = %s AND environment_kind = %s AND access_level = %s AND status = 'published'",
            (dep["template_slug"], env["kind"], lvl["id"]),
        ).fetchone()
        out.append(
            {
                "id": lvl["id"],
                "label": (mapping["access_label"] if mapping else lvl["label"]),
                "eligible": mapping is not None,
                "mapping_version": mapping["version"] if mapping else None,
                # Jamais le nom du groupe côté liste publique de choix.
            }
        )
    return out


def create_request(
    conn: Conn,
    principal: Principal,
    deployment_id: UUID,
    body: dict[str, Any],
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Crée une demande d'accès. Champ `group` / `idp_group` → 422."""
    if "group" in body or "idp_group" in body:
        raise ApiError(
            422,
            "locked_field",
            "le groupe d'identité est résolu côté serveur ; saisie refusée",
            field="/group" if "group" in body else "/idp_group",
        )
    allowed = {"access_level", "subject", "subject_user_id"}
    extra = set(body) - allowed
    if extra:
        key = sorted(extra)[0]
        raise ApiError(422, "unknown_field", f"champ « {key} » refusé", field=f"/{key}")

    dep = get_deployment(conn, principal, deployment_id, for_update=True)
    require_project(principal, dep["project_id"], "déploiement")
    # Tout utilisateur du projet peut demander ; l'opérateur n'est pas obligatoire.
    if not principal.can_see_project(dep["project_id"]):
        raise not_found("déploiement")

    access_level = body.get("access_level")
    if not isinstance(access_level, str) or not access_level.strip():
        raise ApiError(422, "invalid_input", "access_level attendu", field="/access_level")
    access_level = access_level.strip()

    tpl = registry.template_by_id(conn, dep["template_version_id"])
    known = {lvl["id"] for lvl in levels_for_template(tpl["contract"])}
    if access_level not in known:
        raise ApiError(422, "unknown_access_level", "niveau d'accès non prévu pour ce service", field="/access_level")

    env = conn.execute("SELECT * FROM environments WHERE id = %s", (dep["environment_id"],)).fetchone()
    mapping = resolve_mapping(
        conn,
        template_slug=dep["template_slug"],
        environment_kind=env["kind"],
        access_level=access_level,
    )

    subject_mode = body.get("subject") or "self"
    if subject_mode not in ("self", "team"):
        raise ApiError(422, "invalid_subject", "subject attendu : self ou team", field="/subject")
    if subject_mode == "self":
        subject_id = principal.user_id
    else:
        raw = body.get("subject_user_id")
        if not raw:
            raise ApiError(422, "invalid_subject", "subject_user_id requis pour team", field="/subject_user_id")
        try:
            subject_id = UUID(str(raw))
        except ValueError as exc:
            raise ApiError(422, "invalid_subject", "subject_user_id invalide", field="/subject_user_id") from exc
        # Le sujet doit avoir un rôle sur le projet (éligibilité).
        binding = conn.execute(
            "SELECT 1 FROM role_bindings WHERE user_id = %s AND project_id = %s AND revoked_at IS NULL LIMIT 1",
            (subject_id, dep["project_id"]),
        ).fetchone()
        if binding is None:
            raise ApiError(
                422, "subject_not_eligible", "le sujet n'est pas éligible sur ce projet", field="/subject_user_id"
            )

    # Pas de doublon pending/granted pour le même sujet + niveau + déploiement.
    dup = conn.execute(
        "SELECT id, state FROM service_access_requests WHERE deployment_id = %s AND subject_user_id = %s "
        "AND access_level = %s AND state = ANY(%s)",
        (
            dep["id"],
            subject_id,
            access_level,
            ["pending_approval", "approved", "syncing", "granted", "sync_failed"],
        ),
    ).fetchone()
    if dup and dup["state"] == "granted":
        raise ApiError(
            409, "already_granted", "accès déjà accordé pour ce niveau", details={"request_id": str(dup["id"])}
        )
    if dup and dup["state"] in ("pending_approval", "approved", "syncing", "sync_failed"):
        raise ApiError(
            409,
            "request_in_progress",
            "une demande est déjà en cours pour ce niveau",
            details={"request_id": str(dup["id"]), "state": dup["state"]},
        )

    req_id = uuid7()
    idem = f"access:{dep['id']}:{subject_id}:{access_level}:{req_id}"
    row = conn.execute(
        """
        INSERT INTO service_access_requests (
            id, deployment_id, project_id, environment_id, requester_id, subject_user_id,
            access_level, mapping_id, idp_group, state, idempotency_key
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending_approval', %s) RETURNING *
        """,
        (
            req_id,
            dep["id"],
            dep["project_id"],
            dep["environment_id"],
            principal.user_id,
            subject_id,
            access_level,
            mapping["id"],
            mapping["idp_group"],
            idem,
        ),
    ).fetchone()
    events.emit(
        conn,
        type="access.request.created",
        source="request",
        project_id=dep["project_id"],
        correlation_id=dep["id"],
        data={"request_id": str(req_id), "access_level": access_level, "state": "pending_approval"},
        dedupe_key=f"access.request.created:{req_id}",
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="access.request.create",
        outcome="ok",
        project_id=dep["project_id"],
        correlation_id=dep["id"],
        targets={"access_request": str(req_id), "level": access_level},
        request_id=request_id,
    )
    return view(conn, principal, row)


def _get_request(conn: Conn, principal: Principal, request_id: UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM service_access_requests WHERE id = %s FOR UPDATE", (request_id,)).fetchone()
    if row is None or not principal.can_see_project(row["project_id"]):
        raise not_found("demande d'accès")
    return row


def approve(
    conn: Conn,
    principal: Principal,
    request_id: UUID,
    *,
    request_id_http: str | None = None,
) -> dict[str, Any]:
    row = _get_request(conn, principal, request_id)
    require_role(principal, "approver", row["project_id"], row["environment_id"])
    if row["state"] != "pending_approval":
        raise ApiError(409, "invalid_state", f"approbation impossible dans l'état « {row['state']} »")
    if row["requester_id"] == principal.user_id:
        raise ApiError(403, "self_approval", "vous ne pouvez pas approuver votre propre demande d'accès")
    conn.execute(
        "UPDATE service_access_requests SET state = 'approved', approved_by = %s, approved_at = now(), "
        "updated_at = now() WHERE id = %s",
        (principal.user_id, request_id),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="access.request.approve",
        outcome="ok",
        project_id=row["project_id"],
        correlation_id=row["deployment_id"],
        targets={"access_request": str(request_id)},
        request_id=request_id_http,
    )
    return sync_to_idp(conn, principal, request_id, request_id_http=request_id_http)


def reject(
    conn: Conn,
    principal: Principal,
    request_id: UUID,
    *,
    comment: str = "",
    request_id_http: str | None = None,
) -> dict[str, Any]:
    row = _get_request(conn, principal, request_id)
    require_role(principal, "approver", row["project_id"], row["environment_id"])
    if row["state"] != "pending_approval":
        raise ApiError(409, "invalid_state", f"refus impossible dans l'état « {row['state']} »")
    conn.execute(
        "UPDATE service_access_requests SET state = 'rejected', rejected_by = %s, rejected_at = now(), "
        "rejection_comment = %s, updated_at = now() WHERE id = %s",
        (principal.user_id, (comment or "")[:500] or None, request_id),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="access.request.reject",
        outcome="ok",
        project_id=row["project_id"],
        correlation_id=row["deployment_id"],
        targets={"access_request": str(request_id)},
        request_id=request_id_http,
    )
    return view(conn, principal, _get_request(conn, principal, request_id))


def sync_to_idp(
    conn: Conn,
    principal: Principal,
    request_id: UUID,
    *,
    request_id_http: str | None = None,
) -> dict[str, Any]:
    """Envoie l'attribution ; « granted » seulement après confirmation. Relance = même clé."""
    row = _get_request(conn, principal, request_id)
    if row["state"] not in ("approved", "sync_failed", "syncing"):
        raise ApiError(409, "invalid_state", "synchronisation possible après approbation ou échec")
    # Relance : opérateur ou approbateur du projet.
    if not (
        principal.has_role("approver", row["project_id"], row["environment_id"])
        or principal.has_role("operator", row["project_id"], row["environment_id"])
        or principal.has_role("platform_admin")
    ):
        raise ApiError(403, "forbidden", "droit insuffisant pour synchroniser")

    conn.execute(
        "UPDATE service_access_requests SET state = 'syncing', sync_attempts = sync_attempts + 1, "
        "updated_at = now() WHERE id = %s",
        (request_id,),
    )
    subject = conn.execute("SELECT * FROM users WHERE id = %s", (row["subject_user_id"],)).fetchone()
    external = subject.get("external_id") or f"local:{subject['id']}"
    try:
        result = idp_fake.assign_group(
            conn,
            subject_external_id=str(external),
            group=row["idp_group"],
            idempotency_key=row["idempotency_key"],
        )
    except ApiError as exc:
        if exc.code != "idp_sync_failed":
            raise
        conn.execute(
            "UPDATE service_access_requests SET state = 'sync_failed', sync_error = %s, updated_at = now() "
            "WHERE id = %s",
            (str(exc.message)[:500], request_id),
        )
        events.emit(
            conn,
            type="access.request.sync_failed",
            source="system",
            project_id=row["project_id"],
            correlation_id=row["deployment_id"],
            data={"request_id": str(request_id), "error": str(exc.message)[:200]},
            dedupe_key=f"access.request.sync_failed:{request_id}:{row['sync_attempts'] + 1}",
        )
        audit.record(
            conn,
            actor_type="user",
            actor_id=str(principal.user_id),
            action="access.request.sync",
            outcome="error",
            project_id=row["project_id"],
            correlation_id=row["deployment_id"],
            targets={"access_request": str(request_id), "error": str(exc.message)[:200]},
            request_id=request_id_http,
        )
        return view(conn, principal, _get_request(conn, principal, request_id))

    # Confirmation IdP → enregistrement anti-doublon puis état granted.
    existing_asg = conn.execute(
        "SELECT id FROM service_access_assignments WHERE idempotency_key = %s",
        (row["idempotency_key"],),
    ).fetchone()
    if existing_asg is None:
        conn.execute(
            """
            INSERT INTO service_access_assignments (
                id, request_id, deployment_id, subject_user_id, idp_group,
                idp_assignment_id, idempotency_key, simulated
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, true)
            """,
            (
                uuid7(),
                request_id,
                row["deployment_id"],
                row["subject_user_id"],
                row["idp_group"],
                result["assignment_id"],
                row["idempotency_key"],
            ),
        )
    conn.execute(
        "UPDATE service_access_requests SET state = 'granted', idp_assignment_id = %s, sync_error = NULL, "
        "granted_at = COALESCE(granted_at, now()), updated_at = now() WHERE id = %s",
        (result["assignment_id"], request_id),
    )
    events.emit(
        conn,
        type="access.request.granted",
        source="system",
        project_id=row["project_id"],
        correlation_id=row["deployment_id"],
        data={
            "request_id": str(request_id),
            "replayed": result.get("replayed", False),
            "simulated": True,
        },
        dedupe_key=f"access.request.granted:{request_id}",
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="access.request.sync",
        outcome="ok",
        project_id=row["project_id"],
        correlation_id=row["deployment_id"],
        targets={
            "access_request": str(request_id),
            "assignment": result["assignment_id"],
            "replayed": result.get("replayed", False),
            "simulated": True,
        },
        request_id=request_id_http,
    )
    return view(conn, principal, _get_request(conn, principal, request_id))


def list_for_deployment(conn: Conn, principal: Principal, deployment_id: UUID) -> list[dict[str, Any]]:
    dep = get_deployment(conn, principal, deployment_id)
    rows = conn.execute(
        "SELECT * FROM service_access_requests WHERE deployment_id = %s ORDER BY created_at DESC",
        (dep["id"],),
    ).fetchall()
    return [view(conn, principal, r) for r in rows]


def get_request(conn: Conn, principal: Principal, request_id: UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM service_access_requests WHERE id = %s", (request_id,)).fetchone()
    if row is None or not principal.can_see_project(row["project_id"]):
        raise not_found("demande d'accès")
    return view(conn, principal, row)


def _mapping_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "template_slug": row["template_slug"],
        "environment_kind": row["environment_kind"],
        "access_level": row["access_level"],
        "access_label": row["access_label"],
        "version": row["version"],
        "status": row["status"],
        # Groupe visible seulement aux admins qui publient — pas dans la vue demandeur.
        "idp_group": row["idp_group"],
        "published_at": row["published_at"].isoformat() if row.get("published_at") else None,
    }


def view(conn: Conn, principal: Principal, row: dict[str, Any]) -> dict[str, Any]:
    subject = conn.execute("SELECT display_name, email FROM users WHERE id = %s", (row["subject_user_id"],)).fetchone()
    requester = conn.execute("SELECT display_name FROM users WHERE id = %s", (row["requester_id"],)).fetchone()
    can_approve = (
        row["state"] == "pending_approval"
        and principal.has_role("approver", row["project_id"], row["environment_id"])
        and principal.user_id != row["requester_id"]
    )
    can_retry = row["state"] == "sync_failed" and (
        principal.has_role("approver", row["project_id"], row["environment_id"])
        or principal.has_role("operator", row["project_id"], row["environment_id"])
        or principal.has_role("platform_admin")
    )
    # Le groupe IdP n'est jamais exposé au demandeur ; admin plateforme seulement.
    show_group = principal.has_role("platform_admin")
    out: dict[str, Any] = {
        "id": str(row["id"]),
        "deployment_id": str(row["deployment_id"]),
        "project_id": str(row["project_id"]),
        "access_level": row["access_level"],
        "state": row["state"],
        "state_label": STATES.get(row["state"], row["state"]),
        "subject": {
            "user_id": str(row["subject_user_id"]),
            "display_name": subject["display_name"] if subject else None,
            "email": subject["email"] if subject else None,
        },
        "requester": {
            "user_id": str(row["requester_id"]),
            "display_name": requester["display_name"] if requester else None,
        },
        "sync_error": row["sync_error"],
        "sync_attempts": row["sync_attempts"],
        "granted_at": row["granted_at"].isoformat() if row.get("granted_at") else None,
        "approved_at": row["approved_at"].isoformat() if row.get("approved_at") else None,
        "created_at": row["created_at"].isoformat() if isinstance(row["created_at"], datetime) else row["created_at"],
        "allowed_actions": [],
        "notice": (
            "Accès accordé uniquement après confirmation du fournisseur d'identité. Aucun mot de passe n'est révélé."
        ),
        "simulated": True,
    }
    if show_group:
        out["idp_group"] = row["idp_group"]
        out["idp_assignment_id"] = row["idp_assignment_id"]
    else:
        # Jamais le groupe ni un secret.
        out["idp_group"] = None
        out["idp_assignment_id"] = row["idp_assignment_id"] if row["state"] == "granted" else None
    if can_approve:
        out["allowed_actions"].extend(
            [{"action": "approve", "label": "Approuver"}, {"action": "reject", "label": "Refuser"}]
        )
    if can_retry:
        out["allowed_actions"].append({"action": "retry_sync", "label": "Relancer la synchronisation"})
    if row["state"] == "granted":
        out["status_badge"] = "accès accordé"
    elif row["state"] == "sync_failed":
        out["status_badge"] = "échec de synchronisation"
    return out


def card_section(conn: Conn, principal: Principal, dep: dict[str, Any]) -> dict[str, Any]:
    """Bloc fiche service : niveaux éligibles + demandes récentes."""
    return {
        "levels": levels_available(conn, dep),
        "requests": list_for_deployment(conn, principal, dep["id"])[:10],
        "can_request": principal.can_see_project(dep["project_id"]),
        "notice": "Le groupe d'identité est résolu côté serveur. Aucun mot de passe n'est affiché.",
    }
