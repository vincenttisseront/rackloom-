"""File durable PostgreSQL : mise en file, réclamation SKIP LOCKED, bail, heartbeat, époque.

Le bail est un jeton de clôture : chaque réclamation incrémente `lease_epoch` et toute
écriture du worker sur le job exige l'époque qu'il détient. Un worker dont le bail a été
repris ne peut plus rien écrire.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from .. import clock, events
from ..db import Conn, Jsonb
from ..ids import uuid7

JOB_CLASS = {
    "inventory_refresh": "read",
    "tf_plan": "read",
    "tf_apply": "write",
    "tf_state_read": "read",
    "guest_ready": "read",
    "ansible_configure": "write",
    "verify": "read",
    "reconcile": "read",
}

JOB_LABELS = {
    "inventory_refresh": "Collecte de l'inventaire",
    "tf_plan": "Calcul du plan Terraform",
    "tf_apply": "Application du plan approuvé",
    "tf_state_read": "Lecture du state",
    "guest_ready": "Préparation des machines",
    "ansible_configure": "Configuration des logiciels",
    "verify": "Vérification du service",
    "reconcile": "Réconciliation",
}


class LeaseLost(Exception):
    """Le bail a expiré ou a été repris : le worker doit s'arrêter sans écrire."""


def enqueue(conn: Conn, *, kind: str, idempotency_key: str, change: dict[str, Any] | None = None,
            project_id: UUID | None = None, payload: dict[str, Any] | None = None,
            stack_id: UUID | None = None, execution_id: UUID | None = None,
            run_after: datetime | None = None, max_attempts: int = 3, priority: int = 100) -> dict[str, Any]:
    """Idempotent : une même clé renvoie le job existant."""
    existing = conn.execute("SELECT * FROM jobs WHERE idempotency_key = %s", (idempotency_key,)).fetchone()
    if existing:
        return existing
    job_id = uuid7()
    pid = project_id or (change["project_id"] if change else None)
    row = conn.execute(
        """
        INSERT INTO jobs (id, change_id, project_id, execution_id, kind, class, state, stack_id, payload,
                          idempotency_key, priority, run_after, max_attempts)
        VALUES (%s, %s, %s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (job_id, change["id"] if change else None, pid, execution_id, kind, JOB_CLASS[kind], stack_id,
         Jsonb(payload or {}), idempotency_key, priority, run_after or clock.now(), max_attempts),
    ).fetchone()
    events.emit(conn, type="job.queued", source="rackloom", project_id=pid,
                correlation_id=change["id"] if change else None, job_id=job_id,
                data={"kind": kind, "label": JOB_LABELS[kind]}, dedupe_key=f"job.queued:{job_id}")
    return row


def claim(conn: Conn, worker_id: str, lease_seconds: int,
          kinds: list[str] | None = None) -> dict[str, Any] | None:
    """Réclame un job en transaction courte. Renvoie le job avec sa nouvelle époque."""
    query = """
        SELECT id FROM jobs
        WHERE state = 'queued' AND run_after <= now()
    """
    params: list[Any] = []
    if kinds:
        query += " AND kind = ANY(%s)"
        params.append(kinds)
    query += " ORDER BY priority, run_after, id FOR UPDATE SKIP LOCKED LIMIT 1"
    row = conn.execute(query, params).fetchone()
    if row is None:
        return None
    return conn.execute(
        """
        UPDATE jobs SET state = 'leased', lease_owner = %s, lease_epoch = lease_epoch + 1,
               lease_expires_at = now() + make_interval(secs => %s), heartbeat_at = now(),
               attempts = attempts + 1, updated_at = now()
        WHERE id = %s RETURNING *
        """,
        (worker_id, lease_seconds, row["id"]),
    ).fetchone()


def fenced_update(conn: Conn, job: dict[str, Any], sets: str, params: tuple = ()) -> dict[str, Any]:
    """UPDATE conditionné par le propriétaire et l'époque du bail ; lève LeaseLost sinon."""
    row = conn.execute(
        f"UPDATE jobs SET {sets}, updated_at = now() "
        "WHERE id = %s AND lease_owner = %s AND lease_epoch = %s AND state IN ('leased', 'running') "
        "RETURNING *",
        (*params, job["id"], job["lease_owner"], job["lease_epoch"]),
    ).fetchone()
    if row is None:
        raise LeaseLost(str(job["id"]))
    return row


def heartbeat(conn: Conn, job: dict[str, Any], lease_seconds: int) -> dict[str, Any]:
    return fenced_update(conn, job, "heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => %s)",
                         (lease_seconds,))


def expired_leases(conn: Conn) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM jobs WHERE state IN ('leased', 'running') AND lease_expires_at < now() "
        "ORDER BY lease_expires_at FOR UPDATE SKIP LOCKED"
    ).fetchall()


def add_step(conn: Conn, job_id: UUID, name: str, state: str, *, simulated: bool = False,
             summary: dict[str, Any] | None = None) -> None:
    seq = conn.execute("SELECT coalesce(max(seq), 0) + 1 AS n FROM job_steps WHERE job_id = %s",
                       (job_id,)).fetchone()["n"]
    conn.execute(
        "INSERT INTO job_steps (job_id, seq, name, state, simulated, summary, finished_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s <> 'running' THEN now() END)",
        (job_id, seq, name, state, simulated, Jsonb(summary or {}), state),
    )


def retry_later(conn: Conn, job: dict[str, Any], seconds: int) -> None:
    conn.execute(
        "UPDATE jobs SET state = 'queued', lease_owner = NULL, lease_expires_at = NULL, "
        "run_after = now() + make_interval(secs => %s), updated_at = now() WHERE id = %s",
        (seconds, job["id"]),
    )


def lease_deadline(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def new_id() -> UUID:
    return uuid7()
