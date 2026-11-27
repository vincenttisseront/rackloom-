"""Faux fournisseur d'identité local (F59).

Attribution de groupe idempotente. Aucun mot de passe. Observations fictives (D2) :
pas d'annuaire réel. Un flag `fail_next` permet de simuler un échec de sync.
"""

from __future__ import annotations

from typing import Any

from ..db import Conn
from ..errors import ApiError
from ..ids import uuid7

# Mémoire process pour les attributions (en plus de la table) — utile aux tests isolés.
_ASSIGNMENTS: dict[str, dict[str, Any]] = {}


def ensure_state(conn: Conn) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM idp_fake_state WHERE id = 1 FOR UPDATE").fetchone()
    if row is None:
        conn.execute("INSERT INTO idp_fake_state (id) VALUES (1)")
        row = conn.execute("SELECT * FROM idp_fake_state WHERE id = 1 FOR UPDATE").fetchone()
    return row


def set_fail_next(conn: Conn, *, fail: bool, message: str = "échec de synchronisation IdP (simulé)") -> None:
    ensure_state(conn)
    conn.execute(
        "UPDATE idp_fake_state SET fail_next = %s, fail_message = %s, updated_at = now() WHERE id = 1",
        (fail, message[:500]),
    )


def assign_group(
    conn: Conn,
    *,
    subject_external_id: str,
    group: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Attribue le groupe. Même clé → même attribution (pas de doublon)."""
    if idempotency_key in _ASSIGNMENTS:
        return {**_ASSIGNMENTS[idempotency_key], "replayed": True}
    existing = conn.execute(
        "SELECT * FROM service_access_assignments WHERE idempotency_key = %s",
        (idempotency_key,),
    ).fetchone()
    if existing:
        out = {
            "assignment_id": existing["idp_assignment_id"],
            "group": existing["idp_group"],
            "subject": subject_external_id,
            "idempotency_key": idempotency_key,
            "simulated": True,
            "replayed": True,
        }
        _ASSIGNMENTS[idempotency_key] = out
        return out

    state = ensure_state(conn)
    if state["fail_next"]:
        conn.execute("UPDATE idp_fake_state SET fail_next = false, updated_at = now() WHERE id = 1")
        raise ApiError(
            502,
            "idp_sync_failed",
            state["fail_message"] or "échec de synchronisation IdP (simulé)",
        )

    assignment_id = f"idp-fake-{uuid7().hex[:16]}"
    conn.execute("UPDATE idp_fake_state SET assignment_count = assignment_count + 1, updated_at = now() WHERE id = 1")
    out = {
        "assignment_id": assignment_id,
        "group": group,
        "subject": subject_external_id,
        "idempotency_key": idempotency_key,
        "simulated": True,
        "replayed": False,
    }
    _ASSIGNMENTS[idempotency_key] = out
    return out


def clear_memory() -> None:
    """Isole l'état partagé entre tests du même process."""
    _ASSIGNMENTS.clear()


def status(conn: Conn) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM idp_fake_state WHERE id = 1").fetchone()
    return {
        "mode": "simulated",
        "fail_next": bool(row["fail_next"]) if row else False,
        "assignment_count": int(row["assignment_count"]) if row else 0,
        "notice": "Faux IdP local : aucune attribution réelle, aucun mot de passe.",
    }
