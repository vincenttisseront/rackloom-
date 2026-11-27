"""Exceptions de politique versionnées (F10).

Propriétaire, périmètre (projet / environnement) et expiration obligatoires.
Une exception expirée ou révoquée n'a aucun effet. La simulation (policy.simulate)
ne crée ni n'altère aucune exception ni aucun plan.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from .. import audit, clock
from ..db import Conn
from ..errors import ApiError, forbidden, not_found
from ..identity.authz import Principal
from ..ids import uuid7
from ..redact import redact

MAX_TTL = timedelta(days=30)


def _reason(value: Any, field: str = "reason") -> str:
    text = redact(str(value or "").strip())
    if not 3 <= len(text) <= 500:
        raise ApiError(422, "reason_required", "motif obligatoire (3 à 500 caractères)", field=f"/{field}")
    return text


def active(
    conn: Conn,
    *,
    rule_id: str,
    project_id: UUID,
    environment_id: UUID | None = None,
    at=None,
) -> dict[str, Any] | None:
    """Exception encore valide pour la règle et le périmètre (la plus spécifique gagne)."""
    instant = at or clock.now()
    rows = conn.execute(
        """
        SELECT * FROM policy_exceptions
        WHERE rule_id = %s AND project_id = %s AND revoked_at IS NULL AND expires_at > %s
          AND (environment_id IS NULL OR environment_id IS NOT DISTINCT FROM %s)
        ORDER BY environment_id NULLS LAST
        """,
        (rule_id, project_id, instant, environment_id),
    ).fetchall()
    for row in rows:
        if row["environment_id"] is None or row["environment_id"] == environment_id:
            return dict(row)
    return None


def list_for_project(conn: Conn, project_id: UUID, *, include_expired: bool = False) -> list[dict[str, Any]]:
    instant = clock.now()
    rows = conn.execute(
        """
        SELECT e.*, u.display_name AS owner_name, c.display_name AS created_by_name
        FROM policy_exceptions e
        JOIN users u ON u.id = e.owner_user_id
        JOIN users c ON c.id = e.created_by
        WHERE e.project_id = %s
        ORDER BY e.created_at DESC
        """,
        (project_id,),
    ).fetchall()
    out = []
    for r in rows:
        expired = r["expires_at"] <= instant or r["revoked_at"] is not None
        if expired and not include_expired:
            continue
        out.append(
            {
                "id": str(r["id"]),
                "rule_id": r["rule_id"],
                "project_id": str(r["project_id"]),
                "environment_id": str(r["environment_id"]) if r["environment_id"] else None,
                "owner_user_id": str(r["owner_user_id"]),
                "owner_name": r["owner_name"],
                "reason": r["reason"],
                "expires_at": r["expires_at"].isoformat(),
                "created_at": r["created_at"].isoformat(),
                "created_by": r["created_by_name"],
                "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None,
                "active": not expired and r["expires_at"] > instant,
            }
        )
    return out


def create(
    conn: Conn,
    principal: Principal,
    *,
    rule_id: str,
    project_id: UUID,
    environment_id: UUID | None,
    owner_user_id: UUID,
    reason: Any,
    expires_at,
    request_id: str | None = None,
) -> dict[str, Any]:

    if not (principal.is_platform_admin or principal.has_role("project_admin", project_id)):
        raise forbidden("rôle project_admin ou administrateur de plateforme requis")
    text = _reason(reason)
    rid = str(rule_id or "").strip()
    if not 2 <= len(rid) <= 40:
        raise ApiError(422, "invalid_rule", "identifiant de règle invalide", field="/rule_id")
    instant = clock.now()
    if expires_at <= instant:
        raise ApiError(422, "invalid_expiry", "expiration dans le futur requise", field="/expires_at")
    if expires_at > instant + MAX_TTL:
        raise ApiError(422, "ttl_exceeded", "durée maximale 30 jours (D14)", field="/expires_at")
    proj = conn.execute("SELECT id FROM projects WHERE id = %s", (project_id,)).fetchone()
    if proj is None:
        raise not_found("projet")
    if environment_id is not None:
        env = conn.execute(
            "SELECT id FROM environments WHERE id = %s AND project_id = %s",
            (environment_id, project_id),
        ).fetchone()
        if env is None:
            raise ApiError(422, "invalid_environment", "environnement hors du projet", field="/environment_id")
    owner = conn.execute("SELECT id FROM users WHERE id = %s AND status = 'active'", (owner_user_id,)).fetchone()
    if owner is None:
        raise not_found("propriétaire")
    row = conn.execute(
        """
        INSERT INTO policy_exceptions
            (id, rule_id, project_id, environment_id, owner_user_id, reason, expires_at, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (uuid7(), rid, project_id, environment_id, owner_user_id, text, expires_at, principal.user_id),
    ).fetchone()
    assert row is not None
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="policy.exception.create",
        outcome="ok",
        project_id=project_id,
        targets={"exception": str(row["id"]), "rule_id": rid},
        policy_decision={"expires_at": expires_at.isoformat(), "reason": text},
        request_id=request_id,
    )
    return {"id": str(row["id"]), "rule_id": rid, "expires_at": expires_at.isoformat()}


def revoke(
    conn: Conn,
    principal: Principal,
    exception_id: UUID,
    reason: Any,
    request_id: str | None = None,
) -> dict[str, Any]:
    text = _reason(reason, "revoke_reason")
    row = conn.execute("SELECT * FROM policy_exceptions WHERE id = %s FOR UPDATE", (exception_id,)).fetchone()
    if row is None:
        raise not_found("exception")
    if not (principal.is_platform_admin or principal.has_role("project_admin", row["project_id"])):
        if not principal.can_see_project(row["project_id"]):
            raise not_found("exception")
        raise forbidden("rôle project_admin ou administrateur de plateforme requis")
    if row["revoked_at"] is not None:
        raise ApiError(409, "already_revoked", "exception déjà révoquée")
    conn.execute(
        "UPDATE policy_exceptions SET revoked_at = now(), revoked_by = %s, revoke_reason = %s WHERE id = %s",
        (principal.user_id, text, exception_id),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="policy.exception.revoke",
        outcome="ok",
        project_id=row["project_id"],
        targets={"exception": str(exception_id), "rule_id": row["rule_id"]},
        policy_decision={"reason": text},
        request_id=request_id,
    )
    return {"id": str(exception_id), "revoked": True}
