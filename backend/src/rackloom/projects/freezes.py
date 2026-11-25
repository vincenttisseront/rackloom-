"""Gel d'urgence d'un environnement (F28).

Le gel suspend le démarrage des jobs d'écriture ; la règle d'admission R080 le lit au démarrage
effectif de chaque job et dans la transaction qui marque l'envoi au fournisseur. Geler et dégeler
verrouillent la ligne de l'environnement (FOR UPDATE), l'admission la verrouille en partage :
un job ne peut pas être admis d'un côté d'un gel et envoyé de l'autre.

Qui peut geler (décision D8, provisoire) : administrateur de plateforme, ou approbateur du projet
sur cet environnement.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import audit, events
from ..db import Conn
from ..errors import ApiError, forbidden, not_found
from ..identity.authz import Principal
from ..ids import uuid7
from ..redact import redact

WRITE_KINDS = ("tf_apply", "ansible_configure")
ACTIVE_CHANGE_STATES = ("plan_ready", "approved", "executing", "verifying", "partially_succeeded", "uncertain",
                        "reconciling", "blocked")


def active(conn: Conn, environment_id: UUID, *, lock: bool = False) -> dict[str, Any] | None:
    if lock:
        conn.execute("SELECT 1 FROM environments WHERE id = %s FOR SHARE", (environment_id,))
    return conn.execute(
        "SELECT f.*, u.display_name AS frozen_by_name FROM environment_freezes f "
        "JOIN users u ON u.id = f.frozen_by WHERE f.environment_id = %s AND f.lifted_at IS NULL",
        (environment_id,),
    ).fetchone()


def refusal(env: dict[str, Any], freeze: dict[str, Any]) -> str:
    return f"environnement « {env['name']} » gelé par {freeze['frozen_by_name']} : {freeze['reason']}"


def can_manage(principal: Principal, env: dict[str, Any]) -> bool:
    return principal.is_platform_admin or principal.has_role("approver", env["project_id"], env["id"])


def load(conn: Conn, principal: Principal, environment_id: UUID, *, for_update: bool = False) -> dict[str, Any]:
    """Hors périmètre : 404. L'administrateur de plateforme voit tous les environnements (gel d'urgence)."""
    env = conn.execute(
        "SELECT e.*, p.slug AS project_slug, p.name AS project_name FROM environments e "
        "JOIN projects p ON p.id = e.project_id WHERE e.id = %s" + (" FOR UPDATE OF e" if for_update else ""),
        (environment_id,),
    ).fetchone()
    if env is None or not (principal.is_platform_admin or principal.can_see_project(env["project_id"])):
        raise not_found("environnement")
    return env


def write_jobs(conn: Conn, environment_id: UUID) -> dict[str, int]:
    """Jobs d'écriture en attente (bloqués au démarrage) et déjà envoyés (poursuivis et suivis)."""
    row = conn.execute(
        """
        SELECT count(*) FILTER (WHERE j.dispatched_at IS NULL) AS queued,
               count(*) FILTER (WHERE j.dispatched_at IS NOT NULL) AS dispatched
        FROM jobs j JOIN changes c ON c.id = j.change_id
        WHERE c.environment_id = %s AND j.kind = ANY(%s) AND j.state IN ('queued', 'leased', 'running')
        """,
        (environment_id, list(WRITE_KINDS)),
    ).fetchone()
    return {"queued": row["queued"], "dispatched": row["dispatched"]}


def summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"id": row["id"], "reason": row["reason"], "frozen_at": row["frozen_at"], "frozen_by": row["frozen_by_name"]}


def _reason(value: Any, field: str) -> str:
    text = redact(str(value or "").strip())
    if not 3 <= len(text) <= 500:
        raise ApiError(422, "reason_required", "motif obligatoire (3 à 500 caractères)", field=f"/{field}")
    return text


def _authorize(principal: Principal, env: dict[str, Any]) -> None:
    if not can_manage(principal, env):
        raise forbidden("rôle approbateur de l'environnement ou administrateur de plateforme requis pour geler "
                        "ou dégeler")


def freeze(conn: Conn, principal: Principal, environment_id: UUID, reason: Any,
           request_id: str | None = None) -> dict[str, Any]:
    env = load(conn, principal, environment_id, for_update=True)
    _authorize(principal, env)
    text = _reason(reason, "reason")
    if active(conn, env["id"]):
        raise ApiError(409, "already_frozen", f"l'environnement « {env['name']} » est déjà gelé")
    row = conn.execute(
        "INSERT INTO environment_freezes (id, environment_id, project_id, reason, frozen_by) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING *",
        (uuid7(), env["id"], env["project_id"], text, principal.user_id),
    ).fetchone()
    jobs = write_jobs(conn, env["id"])
    data = {"environment": env["name"], "by": principal.display_name, "reason": text,
            "queued_writes": jobs["queued"], "dispatched_writes": jobs["dispatched"]}
    _announce(conn, env, "environment.frozen", row["id"], data, "warning")
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action="environment.freeze",
                 outcome="ok", project_id=env["project_id"],
                 targets={"environment_id": str(env["id"]), "freeze_id": str(row["id"])},
                 policy_decision={"reason": text, "queued_writes": jobs["queued"],
                                  "dispatched_writes": jobs["dispatched"]},
                 request_id=request_id)
    return {"environment_id": env["id"], "freeze_id": row["id"], "write_jobs": jobs}


def unfreeze(conn: Conn, principal: Principal, environment_id: UUID, reason: Any,
             request_id: str | None = None) -> dict[str, Any]:
    env = load(conn, principal, environment_id, for_update=True)
    _authorize(principal, env)
    text = _reason(reason, "reason")
    current = active(conn, env["id"])
    if current is None:
        raise ApiError(409, "not_frozen", f"l'environnement « {env['name']} » n'est pas gelé")
    conn.execute("UPDATE environment_freezes SET lifted_at = now(), lifted_by = %s, lift_reason = %s WHERE id = %s",
                 (principal.user_id, text, current["id"]))
    data = {"environment": env["name"], "by": principal.display_name, "reason": text}
    _announce(conn, env, "environment.unfrozen", current["id"], data, "info")
    audit.record(conn, actor_type="user", actor_id=str(principal.user_id), action="environment.unfreeze",
                 outcome="ok", project_id=env["project_id"],
                 targets={"environment_id": str(env["id"]), "freeze_id": str(current["id"])},
                 policy_decision={"reason": text}, request_id=request_id)
    return {"environment_id": env["id"], "freeze_id": current["id"]}


def _announce(conn: Conn, env: dict[str, Any], type_: str, freeze_id: UUID, data: dict[str, Any],
              severity: str) -> None:
    """Un événement au niveau du projet, et un par demande en cours dans l'environnement pour sa chronologie."""
    events.emit(conn, type=type_, source="system", project_id=env["project_id"], environment_id=env["id"],
                data=data, severity=severity, dedupe_key=f"{type_}:{freeze_id}")
    changes = conn.execute("SELECT id FROM changes WHERE environment_id = %s AND state = ANY(%s)",
                           (env["id"], list(ACTIVE_CHANGE_STATES))).fetchall()
    for change in changes:
        events.emit(conn, type=type_, source="system", project_id=env["project_id"], environment_id=env["id"],
                    correlation_id=change["id"], data=data, severity=severity,
                    dedupe_key=f"{type_}:{freeze_id}:{change['id']}")


def history(conn: Conn, environment_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT f.id, f.reason, f.frozen_at, fu.display_name AS frozen_by, f.lifted_at, f.lift_reason,
               lu.display_name AS lifted_by
        FROM environment_freezes f JOIN users fu ON fu.id = f.frozen_by LEFT JOIN users lu ON lu.id = f.lifted_by
        WHERE f.environment_id = %s ORDER BY f.frozen_at DESC LIMIT %s
        """,
        (environment_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
