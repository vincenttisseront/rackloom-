"""Machine à états d'un changement. Toute écriture d'état passe par `transition()`,
qui enregistre l'historique, l'événement et l'audit dans la même transaction."""


from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import audit, events
from ..db import Conn, Jsonb
from ..errors import ApiError

STATES = {
    "draft": "Brouillon",
    "needs_info": "Précisions requises",
    "resolution_blocked": "Résolution impossible",
    "validated": "Validée",
    "planning": "Plan en cours",
    "plan_failed": "Échec du plan",
    "plan_ready": "Plan prêt",
    "plan_expired": "Plan expiré",
    "rejected": "Refusée",
    "approved": "Approuvée",
    "blocked": "Bloquée à l'admission",
    "executing": "Exécution",
    "verifying": "Vérification",
    "succeeded": "Succès",
    "partially_succeeded": "Succès partiel",
    "failed": "Échec",
    "uncertain": "Résultat incertain",
    "reconciling": "Réconciliation",
    "cancelled": "Annulée",
}


ALLOWED: dict[str, set[str]] = {
    "draft": {"needs_info", "resolution_blocked", "validated", "draft", "cancelled"},
    "needs_info": {"draft", "needs_info", "resolution_blocked", "cancelled"},
    "resolution_blocked": {"draft", "needs_info", "resolution_blocked", "cancelled"},
    "validated": {"planning", "cancelled"},
    # planning → succeeded : nouvelle planification après succès sans aucun changement (T27).
    "planning": {"plan_ready", "plan_failed", "cancelled", "succeeded"},
    "plan_failed": {"draft", "needs_info", "resolution_blocked", "cancelled"},
    "plan_ready": {"approved", "rejected", "plan_expired", "draft", "needs_info", "resolution_blocked", "cancelled"},
    "plan_expired": {"planning", "draft", "needs_info", "resolution_blocked", "cancelled"},
    "rejected": {"draft", "needs_info", "resolution_blocked", "cancelled"},
    "approved": {"executing", "blocked", "plan_expired", "draft", "needs_info", "resolution_blocked", "cancelled"},
    "blocked": {"draft", "needs_info", "resolution_blocked", "cancelled"},
    # executing → plan_expired : Terraform refuse un plan sauvegardé périmé (aucune ressource touchée).
    # executing → cancelled : annulation effective avant toute création de ressource.
    "executing": {"verifying", "failed", "partially_succeeded", "uncertain", "plan_expired", "cancelled"},
    # verifying → executing : vague suivante après la vérification réussie de la précédente (AT01).
    "verifying": {"succeeded", "partially_succeeded", "failed", "uncertain", "executing"},
    "uncertain": {"reconciling"},
    # Une réconciliation concluant « appliqué » reprend les étapes suivantes (invités, configuration).
    "reconciling": {"executing", "verifying", "failed", "uncertain"},
    "partially_succeeded": {"executing"},
    "succeeded": {"planning"},
    "failed": set(),
    "cancelled": set(),
}


EDITABLE = {"draft", "needs_info", "resolution_blocked", "plan_failed", "plan_ready", "plan_expired", "rejected",
            "approved", "blocked"}


BEFORE_EXECUTION = EDITABLE | {"validated", "planning"}


RELEASES_NAMES = {"cancelled", "rejected", "plan_failed", "failed"}


TERMINAL = {"failed", "cancelled"}


def transition(conn: Conn, change: dict[str, Any], to_state: str, *, reason_code: str,
               actor_type: str, actor_id: str | None, detail: dict[str, Any] | None = None,
               severity: str = "info", request_id: str | None = None) -> dict[str, Any]:
    """Transition contrôlée avec verrou optimiste (row_version)."""
    from_state = change["state"]
    if to_state not in ALLOWED.get(from_state, set()):
        raise ApiError(409, "invalid_transition",
                       f"transition interdite : {STATES.get(from_state, from_state)} → "
                       f"{STATES.get(to_state, to_state)}")
    row = conn.execute(
        """
        UPDATE changes SET state = %s, row_version = row_version + 1, updated_at = now()
        WHERE id = %s AND row_version = %s RETURNING *
        """,
        (to_state, change["id"], change["row_version"]),
    ).fetchone()
    if row is None:
        raise ApiError(409, "concurrent_update", "le changement a été modifié entre-temps, recharger")
    conn.execute(
        """
        INSERT INTO state_transitions (change_id, from_state, to_state, reason_code, actor_type, actor_id, detail)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (change["id"], from_state, to_state, reason_code, actor_type, actor_id, Jsonb(detail or {})),
    )
    events.emit(conn, type="change.state", source="rackloom", project_id=change["project_id"],
                correlation_id=change["id"], environment_id=change.get("environment_id"), severity=severity,
                data={"from": from_state, "to": to_state, "label": STATES[to_state], "reason": reason_code,
                      **(detail or {})},
                dedupe_key=f"change.state:{change['id']}:{row['row_version']}")
    if to_state in RELEASES_NAMES:
        release_names(conn, row)
        if row["kind"] == "create" and row["deployment_id"] is not None:
            conn.execute("UPDATE deployments SET status = 'abandoned' WHERE id = %s AND status = 'pending'",
                         (row["deployment_id"],))
    audit.record(conn, actor_type=actor_type, actor_id=actor_id, action="change.transition",
                 outcome=to_state, project_id=change["project_id"], correlation_id=change["id"],
                 targets={"change": str(change["id"])}, versions={"from": from_state, "to": to_state},
                 policy_decision={"reason": reason_code}, request_id=request_id)
    return row


def lock_change(conn: Conn, change_id: UUID) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (change_id,)).fetchone()


def release_names(conn: Conn, change: dict[str, Any]) -> int:
    """Libère les noms réservés par ce changement qui ne désignent aucune ressource créée."""
    rows = conn.execute(
        """
        UPDATE name_reservations r SET released_at = now()
        WHERE r.change_id = %s AND r.released_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM hosts h WHERE h.project_id = r.project_id AND h.name = r.name)
          AND NOT EXISTS (SELECT 1 FROM deployments d, jsonb_array_elements(d.instances) i
                          WHERE d.id = %s AND i->>'name' = r.name)
        RETURNING r.id
        """,
        (change["id"], change.get("deployment_id")),
    ).fetchall()
    return len(rows)
