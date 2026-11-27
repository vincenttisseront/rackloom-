"""F36 : quotas de concurrence et priorité par projet / environnement.

La file PostgreSQL reste globale ; `claim` ignore les jobs dont le projet a déjà
atteint son plafond de jobs `leased`/`running` pour la classe concernée. Un autre
projet sous quota peut démarrer immédiatement.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import policy
from ..db import Conn
from ..ids import uuid7

WAIT_QUOTA = "project_quota_saturated"
WAIT_ENV_QUOTA = "environment_quota_saturated"

ACTIVE_STATES = ("leased", "running")


def default_max(job_class: str) -> int | None:
    """Plafond plateforme ; None = illimité (comportement historique)."""
    key = f"execution_quota_{job_class}_per_project"
    raw = policy.BUILTIN_RULES["platform"].get(key)
    if raw is None:
        return None
    return int(raw)


def set_quota(
    conn: Conn,
    project_id: UUID,
    *,
    job_class: str = "write",
    max_concurrent: int,
    environment_id: UUID | None = None,
    priority: int = 100,
) -> dict[str, Any]:
    """Configure un quota (tests et administration)."""
    if job_class not in ("read", "write", "all"):
        raise ValueError(job_class)
    if max_concurrent < 1:
        raise ValueError("max_concurrent")
    existing = conn.execute(
        "SELECT * FROM execution_quotas WHERE project_id = %s "
        "AND environment_id IS NOT DISTINCT FROM %s AND job_class = %s",
        (project_id, environment_id, job_class),
    ).fetchone()
    if existing:
        return conn.execute(
            "UPDATE execution_quotas SET max_concurrent = %s, priority = %s, updated_at = now() "
            "WHERE id = %s RETURNING *",
            (max_concurrent, priority, existing["id"]),
        ).fetchone()
    return conn.execute(
        "INSERT INTO execution_quotas (id, project_id, environment_id, job_class, max_concurrent, priority) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
        (uuid7(), project_id, environment_id, job_class, max_concurrent, priority),
    ).fetchone()


def clear_quotas(conn: Conn, project_id: UUID) -> None:
    conn.execute("DELETE FROM execution_quotas WHERE project_id = %s", (project_id,))


def _rows_for(conn: Conn, project_id: UUID | None, environment_id: UUID | None, job_class: str) -> list[dict]:
    if project_id is None:
        return []
    return conn.execute(
        "SELECT * FROM execution_quotas WHERE project_id = %s "
        "AND (environment_id IS NULL OR environment_id IS NOT DISTINCT FROM %s) "
        "AND job_class IN (%s, 'all') ORDER BY environment_id NULLS LAST",
        (project_id, environment_id, job_class),
    ).fetchall()


def limit_for(
    conn: Conn, project_id: UUID | None, job_class: str, *, environment_id: UUID | None = None
) -> tuple[int | None, str]:
    """Renvoie (plafond ou None, portée)."""
    rows = _rows_for(conn, project_id, environment_id, job_class)
    env_row = next((r for r in rows if r["environment_id"] is not None), None)
    if env_row is not None:
        return int(env_row["max_concurrent"]), "environment"
    proj_row = next((r for r in rows if r["environment_id"] is None), None)
    if proj_row is not None:
        return int(proj_row["max_concurrent"]), "project"
    return default_max(job_class), "platform"


def priority_for(conn: Conn, project_id: UUID | None, job_class: str = "write") -> int:
    rows = _rows_for(conn, project_id, None, job_class)
    if rows:
        return int(min(r["priority"] for r in rows))
    return 100


def active_count(conn: Conn, project_id: UUID | None, job_class: str, *, environment_id: UUID | None = None) -> int:
    if project_id is None:
        return 0
    if environment_id is not None:
        return conn.execute(
            "SELECT count(*) AS n FROM jobs j "
            "JOIN changes c ON c.id = j.change_id "
            "WHERE j.project_id = %s AND c.environment_id = %s "
            "AND j.state = ANY(%s) AND j.class = %s",
            (project_id, environment_id, list(ACTIVE_STATES), job_class),
        ).fetchone()["n"]
    return conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE project_id = %s AND state = ANY(%s) AND class = %s",
        (project_id, list(ACTIVE_STATES), job_class),
    ).fetchone()["n"]


def admits(conn: Conn, job: dict[str, Any], *, environment_id: UUID | None = None) -> tuple[bool, str | None]:
    """True si le job peut être réclamé sans dépasser le quota."""
    limit, scope = limit_for(conn, job.get("project_id"), job["class"], environment_id=environment_id)
    if limit is None:
        return True, None
    env_filter = environment_id if scope == "environment" else None
    n = active_count(conn, job.get("project_id"), job["class"], environment_id=env_filter)
    if n >= limit:
        return False, WAIT_ENV_QUOTA if scope == "environment" else WAIT_QUOTA
    return True, None


def mark_waiting(conn: Conn, job_id: UUID, reason: str) -> None:
    conn.execute(
        "UPDATE jobs SET wait_reason = %s, updated_at = now() WHERE id = %s AND state = 'queued'",
        (reason, job_id),
    )


def clear_waiting(conn: Conn, job_id: UUID) -> None:
    conn.execute(
        "UPDATE jobs SET wait_reason = NULL, updated_at = now() WHERE id = %s",
        (job_id,),
    )


def waiting_reason_label(code: str | None) -> str | None:
    if not code:
        return None
    from ..recovery import WAIT_DISPATCH_PAUSED

    labels = {
        WAIT_QUOTA: "quota de concurrence du projet saturé ; un autre projet peut démarrer",
        WAIT_ENV_QUOTA: "quota de concurrence de l'environnement saturé",
        WAIT_DISPATCH_PAUSED: (
            "envois vers le parc suspendus (restauration ou maintenance) ; "
            "aucune écriture relancée tant que l'interrupteur reste actif"
        ),
    }
    return labels.get(code, code)


def status_for_change(conn: Conn, change: dict[str, Any]) -> dict[str, Any]:
    """Bloc `queue` pour l'estimation F56 / F36."""
    jobs = conn.execute(
        "SELECT * FROM jobs WHERE change_id = %s AND state = 'queued' ORDER BY created_at",
        (change["id"],),
    ).fetchall()
    if not jobs:
        active = conn.execute(
            "SELECT 1 FROM jobs WHERE change_id = %s AND state = ANY(%s) LIMIT 1",
            (change["id"], list(ACTIVE_STATES)),
        ).fetchone()
        return {
            "status": "running" if active else "idle",
            "reason": "exécution en cours" if active else "aucun job en file",
            "wait_reason": None,
            "quota": None,
            "ahead_count": 0,
        }

    sample = jobs[0]
    env_id = change.get("environment_id")
    limit, scope = limit_for(conn, sample.get("project_id"), sample["class"], environment_id=env_id)
    active = active_count(
        conn,
        sample.get("project_id"),
        sample["class"],
        environment_id=env_id if scope == "environment" else None,
    )
    wait = sample.get("wait_reason")
    ok, inferred = admits(conn, sample, environment_id=env_id)
    if not ok and not wait:
        wait = inferred
        mark_waiting(conn, sample["id"], wait or WAIT_QUOTA)

    ahead = conn.execute(
        """
        SELECT count(*) AS n FROM jobs j
        WHERE j.state = 'queued' AND j.run_after <= now()
          AND (j.priority, j.run_after, j.id) < (%s, %s, %s)
          AND j.id <> %s
        """,
        (sample["priority"], sample["run_after"], sample["id"], sample["id"]),
    ).fetchone()["n"]

    if wait:
        return {
            "status": "waiting",
            "reason": waiting_reason_label(wait),
            "wait_reason": wait,
            "quota": {"limit": limit, "active": active, "scope": scope, "job_class": sample["class"]},
            "ahead_count": ahead,
            "runner_choice": False,
        }
    return {
        "status": "ready",
        "reason": "en file, éligible dès qu'un emplacement worker est libre",
        "wait_reason": None,
        "quota": {"limit": limit, "active": active, "scope": scope, "job_class": sample["class"]},
        "ahead_count": ahead,
        "runner_choice": False,
    }
