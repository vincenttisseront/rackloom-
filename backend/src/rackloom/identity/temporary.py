"""Droits temporaires à la demande (F11).

Élévation limitée à un projet, un environnement optionnel, une opération et une durée.
Attribution, expiration et révocation sont auditées. `load_bindings` fusionne les grants
encore valides ; R100 refuse le démarrage si le droit temporaire a expiré entre l'approbation
et l'admission.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from .. import audit, clock
from ..db import Conn
from ..errors import ApiError, forbidden, not_found
from ..identity.authz import Binding, Principal
from ..ids import uuid7
from ..redact import redact

GRANTABLE = ("operator", "approver", "auditor", "project_admin", "connector_admin")
OPERATIONS = ("any", "approve", "deploy", "configure_connector", "manage_policy")
MAX_TTL = timedelta(days=30)


def _reason(value: Any, field: str = "reason") -> str:
    text = redact(str(value or "").strip())
    if not 3 <= len(text) <= 500:
        raise ApiError(422, "reason_required", "motif obligatoire (3 à 500 caractères)", field=f"/{field}")
    return text


def active_grants(conn: Conn, user_id: UUID, *, at=None) -> list[dict[str, Any]]:
    instant = at or clock.now()
    return list(
        conn.execute(
            """
            SELECT * FROM temporary_grants
            WHERE user_id = %s AND revoked_at IS NULL AND expires_at > %s
            ORDER BY granted_at
            """,
            (user_id, instant),
        ).fetchall()
    )


def as_bindings(conn: Conn, user_id: UUID, *, at=None) -> list[Binding]:
    return [
        Binding(role=g["role"], project_id=g["project_id"], environment_id=g["environment_id"])
        for g in active_grants(conn, user_id, at=at)
    ]


def expired_covering(
    conn: Conn,
    user_id: UUID,
    *,
    role: str,
    project_id: UUID,
    environment_id: UUID | None,
    at=None,
) -> dict[str, Any] | None:
    """Grant qui couvrait le rôle mais n'est plus valide (expiré ou révoqué)."""
    instant = at or clock.now()
    rows = conn.execute(
        """
        SELECT * FROM temporary_grants
        WHERE user_id = %s AND role = %s AND project_id = %s
          AND (environment_id IS NULL OR environment_id IS NOT DISTINCT FROM %s)
          AND (revoked_at IS NOT NULL OR expires_at <= %s)
        ORDER BY expires_at DESC
        """,
        (user_id, role, project_id, environment_id, instant),
    ).fetchall()
    return dict(rows[0]) if rows else None


def grant(
    conn: Conn,
    principal: Principal,
    *,
    user_id: UUID,
    role: str,
    project_id: UUID,
    environment_id: UUID | None,
    operation: str,
    reason: Any,
    expires_at,
    request_id: str | None = None,
) -> dict[str, Any]:
    if role not in GRANTABLE:
        raise ApiError(422, "invalid_role", f"rôle attendu : {', '.join(GRANTABLE)}", field="/role")
    if operation not in OPERATIONS:
        raise ApiError(422, "invalid_operation", f"opération attendue : {', '.join(OPERATIONS)}", field="/operation")
    if not (principal.is_platform_admin or principal.has_role("project_admin", project_id)):
        raise forbidden("rôle project_admin ou administrateur de plateforme requis")
    text = _reason(reason)
    instant = clock.now()
    if expires_at <= instant:
        raise ApiError(422, "invalid_expiry", "expiration dans le futur requise", field="/expires_at")
    if expires_at > instant + MAX_TTL:
        raise ApiError(422, "ttl_exceeded", "durée maximale 30 jours (D14)", field="/expires_at")
    if conn.execute("SELECT 1 FROM users WHERE id = %s AND status = 'active'", (user_id,)).fetchone() is None:
        raise not_found("utilisateur")
    if conn.execute("SELECT 1 FROM projects WHERE id = %s", (project_id,)).fetchone() is None:
        raise not_found("projet")
    if environment_id is not None:
        env = conn.execute(
            "SELECT id FROM environments WHERE id = %s AND project_id = %s",
            (environment_id, project_id),
        ).fetchone()
        if env is None:
            raise ApiError(422, "invalid_environment", "environnement hors du projet", field="/environment_id")
    row = conn.execute(
        """
        INSERT INTO temporary_grants
            (id, user_id, role, project_id, environment_id, operation, reason, granted_by, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (uuid7(), user_id, role, project_id, environment_id, operation, text, principal.user_id, expires_at),
    ).fetchone()
    assert row is not None
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="temporary_grant.create",
        outcome="ok",
        project_id=project_id,
        targets={"grant": str(row["id"]), "user": str(user_id), "role": role, "operation": operation},
        policy_decision={"expires_at": expires_at.isoformat(), "reason": text},
        request_id=request_id,
    )
    return {
        "id": str(row["id"]),
        "user_id": str(user_id),
        "role": role,
        "operation": operation,
        "expires_at": expires_at.isoformat(),
    }


def revoke(
    conn: Conn,
    principal: Principal,
    grant_id: UUID,
    reason: Any,
    request_id: str | None = None,
) -> dict[str, Any]:
    text = _reason(reason, "revoke_reason")
    row = conn.execute("SELECT * FROM temporary_grants WHERE id = %s FOR UPDATE", (grant_id,)).fetchone()
    if row is None:
        raise not_found("droit temporaire")
    if not (principal.is_platform_admin or principal.has_role("project_admin", row["project_id"])):
        if not principal.can_see_project(row["project_id"]):
            raise not_found("droit temporaire")
        raise forbidden("rôle project_admin ou administrateur de plateforme requis")
    if row["revoked_at"] is not None:
        raise ApiError(409, "already_revoked", "droit déjà révoqué")
    conn.execute(
        "UPDATE temporary_grants SET revoked_at = now(), revoked_by = %s, revoke_reason = %s WHERE id = %s",
        (principal.user_id, text, grant_id),
    )
    from . import service as identity

    identity.bump_privilege_epoch(conn, row["user_id"], "temporary_grant_revoked")
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="temporary_grant.revoke",
        outcome="ok",
        project_id=row["project_id"],
        targets={"grant": str(grant_id), "user": str(row["user_id"]), "role": row["role"]},
        policy_decision={"reason": text},
        request_id=request_id,
    )
    return {"id": str(grant_id), "revoked": True}


def list_for_project(conn: Conn, project_id: UUID, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    instant = clock.now()
    rows = conn.execute(
        """
        SELECT g.*, u.email, u.display_name, gb.display_name AS granted_by_name
        FROM temporary_grants g
        JOIN users u ON u.id = g.user_id
        JOIN users gb ON gb.id = g.granted_by
        WHERE g.project_id = %s
        ORDER BY g.granted_at DESC
        """,
        (project_id,),
    ).fetchall()
    out = []
    for r in rows:
        active = r["revoked_at"] is None and r["expires_at"] > instant
        if not active and not include_inactive:
            continue
        out.append(
            {
                "id": str(r["id"]),
                "user_id": str(r["user_id"]),
                "email": r["email"],
                "display_name": r["display_name"],
                "role": r["role"],
                "operation": r["operation"],
                "environment_id": str(r["environment_id"]) if r["environment_id"] else None,
                "reason": r["reason"],
                "granted_by": r["granted_by_name"],
                "granted_at": r["granted_at"].isoformat(),
                "expires_at": r["expires_at"].isoformat(),
                "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None,
                "active": active,
            }
        )
    return out
