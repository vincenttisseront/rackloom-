"""Machine à états d'un changement. Toute écriture d'état passe par `transition()`,
qui enregistre l'historique, l'événement et l'audit dans la même transaction."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from .. import audit, events
from ..db import Jsonb
from ..errors import ApiError

STATES = {
    "draft": "Brouillon",
    "needs_info": "Précisions requises",
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
    "draft": {"needs_info", "validated", "draft", "cancelled"},
    "needs_info": {"draft", "needs_info", "validated", "cancelled"},
    "validated": {"planning", "cancelled"},
    # planning → succeeded : nouvelle planification après succès sans aucun changement (T27).
    "planning": {"plan_ready", "plan_failed", "cancelled", "succeeded"},
    "plan_failed": {"draft", "needs_info", "cancelled"},
    "plan_ready": {"approved", "rejected", "plan_expired", "draft", "needs_info", "cancelled"},
    "plan_expired": {"planning", "draft", "needs_info", "cancelled"},
    "rejected": {"draft", "needs_info", "cancelled"},
    "approved": {"executing", "blocked", "plan_expired", "draft", "needs_info", "cancelled"},
    "blocked": {"planning", "draft", "needs_info", "cancelled"},
    # executing → plan_expired : Terraform refuse un plan sauvegardé périmé (aucune ressource touchée).
    # executing → cancelled : annulation effective avant toute création de ressource.
    "executing": {"verifying", "failed", "partially_succeeded", "uncertain", "succeeded", "plan_expired",
                  "cancelled"},
    "verifying": {"succeeded", "partially_succeeded", "failed", "uncertain"},
    "uncertain": {"reconciling"},
    # Écart au diagramme d'architecture : une réconciliation concluant « appliqué » reprend
    # les étapes suivantes (invités, configuration) avant la vérification.
    "reconciling": {"executing", "verifying", "failed", "uncertain"},
    # Révision d'une demande déjà déployée : le nouveau plan montre l'écart (remplacement bloqué).
    "partially_succeeded": {"executing", "draft", "needs_info"},
    "succeeded": {"planning", "draft", "needs_info"},
    "failed": set(),
    "cancelled": set(),
}

EDITABLE = {"draft", "needs_info", "plan_failed", "plan_ready", "plan_expired", "rejected", "approved", "blocked",
            "succeeded", "partially_succeeded"}
TERMINAL = {"failed", "cancelled"}


def transition(conn: psycopg.Connection, change: dict[str, Any], to_state: str, *, reason_code: str,
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
    audit.record(conn, actor_type=actor_type, actor_id=actor_id, action="change.transition",
                 outcome=to_state, project_id=change["project_id"], correlation_id=change["id"],
                 targets={"change": str(change["id"])}, versions={"from": from_state, "to": to_state},
                 policy_decision={"reason": reason_code}, request_id=request_id)
    return row


def lock_change(conn: psycopg.Connection, change_id: UUID) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (change_id,)).fetchone()
