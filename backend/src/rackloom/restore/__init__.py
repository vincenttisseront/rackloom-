"""F40 : restauration d'une VM en réseau isolé (quarantaine).

Le résolveur utilise `restore.profile` (pur). Ce module orchestre le workflow
(changement, file `vm_restore`). Exécution simulée locale : pas de labo L2.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import UUID

from ..changes.service import get_deployment
from ..changes.transitions import EDITABLE
from ..db import Conn
from ..errors import ApiError
from ..executions import queue
from ..identity.authz import Principal, require_role
from ..ids import uuid7
from .profile import (
    QUARANTINE_ROLE,
    assert_isolated_network,
    restore_profile,
    verify_plan,
)

__all__ = [
    "QUARANTINE_ROLE",
    "assert_isolated_network",
    "create_restore_change",
    "enqueue_restore",
    "restore_profile",
    "verify_plan",
]


def create_restore_change(
    conn: Conn,
    principal: Principal,
    deployment_id: UUID,
    *,
    source_instance: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Crée une demande de restauration liée au déploiement source."""
    from ..changes import service

    dep = get_deployment(conn, principal, deployment_id, for_update=True)
    require_role(principal, "operator", dep["project_id"], dep["environment_id"])
    if dep["status"] != "active":
        raise ApiError(409, "deployment_not_active", "seul un déploiement actif peut être restauré")
    names = {i["name"] for i in (dep["instances"] or [])}
    if source_instance not in names:
        raise ApiError(
            422,
            "unknown_instance",
            f"instance « {source_instance} » absente du déploiement",
            field="/inputs/source_instance",
        )
    busy_states = (service.ACTIVE_STATES | EDITABLE) - {"rejected", "plan_failed"}
    busy = conn.execute(
        "SELECT id FROM changes WHERE deployment_id = %s AND state = ANY(%s)",
        (deployment_id, list(busy_states)),
    ).fetchone()
    if busy:
        raise ApiError(
            409,
            "change_in_progress",
            "un changement est déjà en cours sur ce déploiement",
            details={"change_id": str(busy["id"])},
        )
    title = f"Restaurer {source_instance} (réseau isolé)"[:120]
    row = conn.execute(
        "INSERT INTO changes (id, kind, deployment_id, project_id, environment_id, title, state, created_by) "
        "VALUES (%s, 'restore', %s, %s, %s, %s, 'draft', %s) RETURNING *",
        (uuid7(), deployment_id, dep["project_id"], dep["environment_id"], title, principal.user_id),
    ).fetchone()
    service._created(conn, principal, row, request_id)
    env_slug = conn.execute(
        "SELECT slug FROM environments WHERE id = %s", (dep["environment_id"],)
    ).fetchone()["slug"]
    need = {
        "schema": "rackloom.need/v1",
        "service": {"template": dep["template_slug"]},
        "environment": env_slug,
        "inputs": {
            "source_instance": source_instance,
            "quantity": 1,
            "lifetime_until": (date.today() + timedelta(days=7)).isoformat(),
        },
        "confirm_production": False,
    }
    service.add_version(
        conn,
        principal,
        row["id"],
        {"need": need, "base_version_no": 0, "source": "api"},
        request_id,
    )
    return conn.execute("SELECT * FROM changes WHERE id = %s", (row["id"],)).fetchone()


def enqueue_restore(conn: Conn, change: dict[str, Any], *, approval_id: UUID | None = None) -> dict[str, Any]:
    """Après approbation : job simulé de restauration en quarantaine."""
    return queue.enqueue(
        conn,
        kind="vm_restore",
        idempotency_key=f"vm_restore:{change['id']}:{change['current_revision_no']}",
        change=change,
        payload={"approval_id": str(approval_id) if approval_id else None, "simulated": True},
        priority=80,
    )
