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
    "preflight": "read",
    "tf_plan": "read",
    "tf_apply": "write",
    "tf_state_read": "read",
    "guest_ready": "read",
    "ansible_configure": "write",
    "verify": "read",
    "reconcile": "read",
    "drift_scan": "read",
    "compliance_scan": "read",
    "vm_restore": "write",
    "service_restart": "write",
    "lease_retire": "write",
    "tf_import": "write",
    "host_patch": "write",
    "patch_check": "read",
}

JOB_LABELS = {
    "inventory_refresh": "Collecte de l'inventaire",
    "preflight": "Préflight (accès et prérequis)",
    "tf_plan": "Calcul du plan Terraform",
    "tf_apply": "Application du plan approuvé",
    "tf_state_read": "Lecture du state",
    "guest_ready": "Préparation des machines",
    "ansible_configure": "Configuration des logiciels",
    "verify": "Vérification du service",
    "reconcile": "Réconciliation",
    "drift_scan": "Détection de dérive (lecture seule)",
    "compliance_scan": "Scan de conformité (lecture seule)",
    "vm_restore": "Restauration VM (réseau isolé)",
    "service_restart": "Redémarrage de service (simulé)",
    "lease_retire": "Retrait de labo (simulé)",
    "tf_import": "Import Terraform (adoption, simulé)",
    "host_patch": "Patch hôte (vague, simulé)",
    "patch_check": "Contrôle de sonde (patch)",
}


class LeaseLost(Exception):
    """Le bail a expiré ou a été repris : le worker doit s'arrêter sans écrire."""


def enqueue(
    conn: Conn,
    *,
    kind: str,
    idempotency_key: str,
    change: dict[str, Any] | None = None,
    project_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
    stack_id: UUID | None = None,
    execution_id: UUID | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
    priority: int | None = None,
) -> dict[str, Any]:
    """Idempotent : une même clé renvoie le job existant.

    F36 : la priorité par défaut suit le quota projet (plus petit = plus tôt).
    """
    from . import quotas as fair

    existing = conn.execute("SELECT * FROM jobs WHERE idempotency_key = %s", (idempotency_key,)).fetchone()
    if existing:
        return existing
    job_id = uuid7()
    pid = project_id or (change["project_id"] if change else None)
    prio = priority if priority is not None else fair.priority_for(conn, pid, JOB_CLASS[kind])
    # F16 : zone requise dérivée de l'environnement (le demandeur ne la choisit pas).
    from .. import runners as zone_runners

    required_zone = zone_runners.required_zone_for_change(conn, change)
    row = conn.execute(
        """
        INSERT INTO jobs (id, change_id, project_id, execution_id, kind, class, state, stack_id, payload,
                          idempotency_key, priority, run_after, max_attempts, required_zone)
        VALUES (%s, %s, %s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (
            job_id,
            change["id"] if change else None,
            pid,
            execution_id,
            kind,
            JOB_CLASS[kind],
            stack_id,
            Jsonb(payload or {}),
            idempotency_key,
            prio,
            run_after or clock.now(),
            max_attempts,
            required_zone,
        ),
    ).fetchone()
    events.emit(
        conn,
        type="job.queued",
        source="job",
        project_id=pid,
        correlation_id=change["id"] if change else None,
        job_id=job_id,
        data={"kind": kind, "label": JOB_LABELS[kind]},
        dedupe_key=f"job.queued:{job_id}",
    )
    assert row is not None
    return row


def claim(
    conn: Conn,
    worker_id: str,
    lease_seconds: int,
    kinds: list[str] | None = None,
    *,
    zone: str | None = None,
    capabilities: list[str] | None = None,
) -> dict[str, Any] | None:
    """Réclame un job en transaction courte. Renvoie le job avec sa nouvelle époque.

    F36 : saute les jobs dont le projet (ou l'environnement) a atteint son quota de
    concurrence ; un autre projet sous quota peut démarrer sans attendre.
    F16 : si `zone` est fournie, saute les jobs dont `required_zone` diffère (un runner
    de dev ne prend jamais un job de prod). `capabilities` borne les kinds acceptés.
    F26 : saute les jobs d'écriture lorsque `dispatch_paused` est actif (restauration /
    maintenance) ; ils restent `queued` avec `wait_reason = dispatch_paused`.
    """
    from .. import recovery
    from . import quotas as fair

    paused = recovery.is_paused(conn)
    skipped: list[Any] = []
    for _ in range(64):
        query = """
            SELECT j.id, j.project_id, j.class, j.change_id, j.priority, j.run_after,
                   j.kind, j.required_zone
            FROM jobs j
            WHERE j.state = 'queued' AND j.run_after <= now()
        """
        params: list[Any] = []
        if kinds:
            query += " AND j.kind = ANY(%s)"
            params.append(kinds)
        if capabilities is not None:
            query += " AND j.kind = ANY(%s)"
            params.append(capabilities)
        if zone is not None:
            # Jobs système sans zone requise : admissibles ; sinon zone exacte.
            query += " AND (j.required_zone IS NULL OR j.required_zone = %s)"
            params.append(zone)
        if skipped:
            query += " AND NOT (j.id = ANY(%s))"
            params.append(skipped)
        query += " ORDER BY j.priority, j.run_after, j.id FOR UPDATE SKIP LOCKED LIMIT 1"
        row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        if paused and row["class"] == "write":
            fair.mark_waiting(conn, row["id"], recovery.WAIT_DISPATCH_PAUSED)
            skipped.append(row["id"])
            continue
        env_id = None
        if row["change_id"] is not None:
            env = conn.execute("SELECT environment_id FROM changes WHERE id = %s", (row["change_id"],)).fetchone()
            env_id = env["environment_id"] if env else None
        ok, reason = fair.admits(conn, row, environment_id=env_id)
        if not ok:
            fair.mark_waiting(conn, row["id"], reason or fair.WAIT_QUOTA)
            skipped.append(row["id"])
            continue
        fair.clear_waiting(conn, row["id"])
        return conn.execute(
            """
            UPDATE jobs SET state = 'leased', lease_owner = %s, lease_epoch = lease_epoch + 1,
                   lease_expires_at = now() + make_interval(secs => %s), heartbeat_at = now(),
                   attempts = attempts + 1, wait_reason = NULL, updated_at = now()
            WHERE id = %s RETURNING *
            """,
            (worker_id, lease_seconds, row["id"]),
        ).fetchone()
    return None


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
    return fenced_update(
        conn, job, "heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => %s)", (lease_seconds,)
    )


def expired_leases(conn: Conn) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM jobs WHERE state IN ('leased', 'running') AND lease_expires_at < now() "
        "ORDER BY lease_expires_at FOR UPDATE SKIP LOCKED"
    ).fetchall()


def add_step(
    conn: Conn, job_id: UUID, name: str, state: str, *, simulated: bool = False, summary: dict[str, Any] | None = None
) -> None:
    seq_row = conn.execute(
        "SELECT coalesce(max(seq), 0) + 1 AS n FROM job_steps WHERE job_id = %s", (job_id,)
    ).fetchone()
    assert seq_row is not None
    seq = seq_row["n"]
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
