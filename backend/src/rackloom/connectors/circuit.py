"""Disjoncteur par connecteur (F37).

Après des échecs répétés, le disjoncteur s'ouvre : les nouvelles écritures qui dépendent
du connecteur sont refusées à l'admission (`connector_circuit_open`). Les lectures restent
admises. Un job déjà envoyé au fournisseur n'est pas annulé ici : s'il devient incertain,
la réconciliation (lecture seule) doit aboutir avant toute fermeture et relance.

Seule une vérification de santé réussie peut fermer le disjoncteur, et seulement s'il
ne reste aucune écriture `uncertain` liée à ce connecteur.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import audit, clock, events, policy
from ..db import Conn
from ..errors import ApiError
from ..redact import redact

# Jobs d'écriture qui dépendent d'un kind de connecteur (admission R095).
WRITE_JOB_CONNECTORS: dict[str, tuple[str, ...]] = {
    "tf_apply": ("terraform", "vsphere"),
    "ansible_configure": ("ansible",),
}

CONNECTOR_WRITE_KINDS: dict[str, tuple[str, ...]] = {
    "vsphere": ("tf_apply",),
    "terraform": ("tf_apply",),
    "ansible": ("ansible_configure",),
}


def failure_threshold() -> int:
    return int(policy.BUILTIN_RULES["platform"]["connector_circuit_failure_threshold"])


def required_kinds(job_kind: str) -> tuple[str, ...]:
    return WRITE_JOB_CONNECTORS.get(job_kind, ())


def open_connectors(conn: Conn, kinds: tuple[str, ...], *, lock: bool = False) -> list[dict[str, Any]]:
    """Connecteurs ouverts parmi les kinds demandés (verrou partageable pour l'admission)."""
    if not kinds:
        return []
    if lock:
        conn.execute(
            "SELECT 1 FROM connectors WHERE kind = ANY(%s) AND circuit_state = 'open' FOR SHARE",
            (list(kinds),),
        )
    return list(
        conn.execute(
            "SELECT * FROM connectors WHERE kind = ANY(%s) AND circuit_state = 'open' ORDER BY slug",
            (list(kinds),),
        ).fetchall()
    )


def refusal(row: dict[str, Any]) -> str:
    reason = row.get("circuit_opened_reason") or "erreurs répétées"
    return (
        f"disjoncteur ouvert sur le connecteur « {row['slug']} » ({row['kind']}) : {reason}. "
        "Nouvelles écritures suspendues ; réconcilier les résultats incertains puis tester la santé "
        "avant reprise."
    )


def uncertain_writes(conn: Conn, connector_kind: str) -> int:
    kinds = CONNECTOR_WRITE_KINDS.get(connector_kind, ())
    if not kinds:
        return 0
    row = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE class = 'write' AND state = 'uncertain' AND kind = ANY(%s)",
        (list(kinds),),
    ).fetchone()
    assert row is not None
    return int(row["n"])


def write_jobs(conn: Conn, connector_kind: str) -> dict[str, int]:
    """Écritures liées au connecteur : en attente (non envoyées) vs déjà envoyées."""
    kinds = CONNECTOR_WRITE_KINDS.get(connector_kind, ())
    if not kinds:
        return {"queued": 0, "dispatched": 0, "uncertain": 0}
    row = conn.execute(
        """
        SELECT count(*) FILTER (WHERE state IN ('queued', 'leased', 'running') AND dispatched_at IS NULL) AS queued,
               count(*) FILTER (WHERE state IN ('queued', 'leased', 'running') AND dispatched_at IS NOT NULL)
                   AS dispatched,
               count(*) FILTER (WHERE state = 'uncertain') AS uncertain
        FROM jobs WHERE class = 'write' AND kind = ANY(%s)
        """,
        (list(kinds),),
    ).fetchone()
    assert row is not None
    return {"queued": row["queued"], "dispatched": row["dispatched"], "uncertain": row["uncertain"]}


def summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": row["circuit_state"],
        "consecutive_failures": row["consecutive_failures"],
        "opened_at": row.get("circuit_opened_at"),
        "opened_reason": row.get("circuit_opened_reason"),
        "error_count": row["error_count"],
    }


def _reason(value: Any) -> str:
    text = redact(str(value or "").strip())
    if not 3 <= len(text) <= 500:
        raise ApiError(422, "reason_required", "motif obligatoire (3 à 500 caractères)", field="/reason")
    return text


def open_circuit(
    conn: Conn,
    connector_id: UUID,
    reason: Any,
    *,
    actor_type: str = "system",
    actor_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Ouvre le disjoncteur (idempotent si déjà ouvert)."""
    text = _reason(reason)
    row = conn.execute("SELECT * FROM connectors WHERE id = %s FOR UPDATE", (connector_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "connecteur introuvable")
    if row["circuit_state"] == "open":
        return {"connector": row, "opened": False, "write_jobs": write_jobs(conn, row["kind"])}
    updated = conn.execute(
        "UPDATE connectors SET circuit_state = 'open', circuit_opened_at = now(), "
        "circuit_opened_reason = %s WHERE id = %s RETURNING *",
        (text, connector_id),
    ).fetchone()
    assert updated is not None
    jobs = write_jobs(conn, updated["kind"])
    data = {
        "connector": updated["slug"],
        "kind": updated["kind"],
        "reason": text,
        "queued_writes": jobs["queued"],
        "dispatched_writes": jobs["dispatched"],
        "uncertain_writes": jobs["uncertain"],
    }
    events.emit(
        conn,
        type="connector.circuit_opened",
        source="system",
        data=data,
        severity="warning",
        dedupe_key=f"connector.circuit_opened:{updated['id']}:{updated['circuit_opened_at'].isoformat()}",
    )
    audit.record(
        conn,
        actor_type=actor_type,
        actor_id=actor_id,
        action="connector.circuit.open",
        outcome="ok",
        targets={"connector": updated["slug"], "kind": updated["kind"]},
        policy_decision=data,
        request_id=request_id,
    )
    return {"connector": updated, "opened": True, "write_jobs": jobs}


def record_failure(
    conn: Conn, connector_id: UUID, reason: str, *, actor_type: str = "system", actor_id: str | None = None
) -> dict[str, Any]:
    """Incrémente les compteurs ; ouvre le disjoncteur au seuil."""
    text = _reason(reason)
    row = conn.execute(
        "UPDATE connectors SET consecutive_failures = consecutive_failures + 1, "
        "error_count = error_count + 1 WHERE id = %s RETURNING *",
        (connector_id,),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "connecteur introuvable")
    threshold = failure_threshold()
    if row["circuit_state"] == "closed" and row["consecutive_failures"] >= threshold:
        return open_circuit(
            conn,
            connector_id,
            f"seuil atteint ({row['consecutive_failures']} échecs) : {text}",
            actor_type=actor_type,
            actor_id=actor_id,
        )
    return {"connector": row, "opened": False, "write_jobs": write_jobs(conn, row["kind"])}


def record_failure_for_kind(
    conn: Conn, kind: str, reason: str, *, actor_type: str = "system", actor_id: str | None = None
) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id FROM connectors WHERE kind = %s ORDER BY slug", (kind,)).fetchall()
    return [record_failure(conn, r["id"], reason, actor_type=actor_type, actor_id=actor_id) for r in rows]


def note_success(
    conn: Conn,
    connector_id: UUID,
    *,
    actor_type: str = "user",
    actor_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Santé OK : remet les échecs à zéro ; tente de fermer si ouvert (après réconciliation)."""
    row = conn.execute("SELECT * FROM connectors WHERE id = %s FOR UPDATE", (connector_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "connecteur introuvable")
    if row["circuit_state"] == "closed":
        updated = conn.execute(
            "UPDATE connectors SET consecutive_failures = 0 WHERE id = %s RETURNING *",
            (connector_id,),
        ).fetchone()
        assert updated is not None
        return {"connector": updated, "closed": False, "already_closed": True, "blocked_by_uncertain": 0}

    pending = uncertain_writes(conn, row["kind"])
    if pending:
        return {
            "connector": row,
            "closed": False,
            "already_closed": False,
            "blocked_by_uncertain": pending,
            "message": (
                f"{pending} écriture(s) incertaine(s) à réconcilier avant de fermer le disjoncteur « {row['slug']} »"
            ),
        }

    updated = conn.execute(
        "UPDATE connectors SET circuit_state = 'closed', consecutive_failures = 0, "
        "circuit_opened_at = NULL, circuit_opened_reason = NULL WHERE id = %s RETURNING *",
        (connector_id,),
    ).fetchone()
    assert updated is not None
    data = {"connector": updated["slug"], "kind": updated["kind"]}
    events.emit(
        conn,
        type="connector.circuit_closed",
        source="system",
        data=data,
        severity="info",
        dedupe_key=f"connector.circuit_closed:{updated['id']}:{clock.now().isoformat()}",
    )
    audit.record(
        conn,
        actor_type=actor_type,
        actor_id=actor_id,
        action="connector.circuit.close",
        outcome="ok",
        targets={"connector": updated["slug"], "kind": updated["kind"]},
        policy_decision=data,
        request_id=request_id,
    )
    return {"connector": updated, "closed": True, "already_closed": False, "blocked_by_uncertain": 0}
