"""F55 : boîte « À moi d'agir » / « En attente d'une autre personne ».

Chaque entrée est calculée côté serveur via `workflow.available_actions` et les droits
courants. Ce n'est pas un second tableau de bord : uniquement les prochaines actions
humaines sur les demandes visibles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .. import workflow
from ..changes.service import change_ref
from ..changes.transitions import STATES
from ..db import Conn
from ..identity.authz import Principal

# Actions qui placent une entrée dans « À moi d'agir » (prochaine étape métier).
# Annuler / dupliquer restent sur la fiche de demande, pas dans la boîte.
MINE_ACTIONS = frozenset(
    {
        "submit",
        "edit",
        "approve",
        "reject",
        "request_retry",
        "approve_retry",
        "reject_retry",
        "replan",
    }
)

# Ordre de priorité pour l'action proposée dans la boîte.
NEXT_PRIORITY = (
    "approve",
    "reject",
    "approve_retry",
    "reject_retry",
    "submit",
    "request_retry",
    "edit",
    "replan",
)

INBOX_LABELS = {
    "approve": "Examiner le plan",
    "reject": "Refuser le plan",
    "approve_retry": "Approuver la reprise",
    "reject_retry": "Refuser la reprise",
    "submit": "Soumettre la demande",
    "edit": "Compléter le besoin",
    "request_retry": "Demander une reprise",
    "replan": "Nouvelle planification",
}

ROLE_LABELS = {
    "operator": "Opérateur",
    "approver": "Approbateur",
    "auditor": "Auditeur",
    "platform_admin": "Administrateur",
}

# États où quelqu'un d'autre (ou un rôle) doit agir — visible en « En attente ».
WAITING_HINTS = {
    "plan_ready": ("En attente d'approbation", ("approver",)),
    "needs_info": ("Précisions requises du demandeur", ("operator",)),
    "partially_succeeded": ("Reprise de configuration à décider", ("operator", "approver")),
}


def _blocker_for_mine(state: str, action: str) -> str:
    if action in ("approve", "reject"):
        return "Plan prêt — à examiner"
    if action in ("approve_retry", "reject_retry"):
        return "Reprise en attente de décision"
    if action == "submit":
        return "Demande prête à soumettre"
    if action == "edit":
        return "Le besoin doit être complété"
    if action == "request_retry":
        return "Succès partiel — reprise possible"
    if action == "replan":
        return "Nouvelle planification possible"
    return STATES.get(state, state)


def _who_can_act(conn: Conn, project_id, environment_id, roles: tuple[str, ...]) -> list[str]:
    """Noms des personnes ayant l'un des rôles sur le projet (ou l'environnement)."""
    if not roles:
        return []
    rows = conn.execute(
        "SELECT DISTINCT u.display_name FROM role_bindings b "
        "JOIN users u ON u.id = b.user_id "
        "WHERE b.revoked_at IS NULL AND b.role = ANY(%s) AND b.project_id = %s "
        "AND (b.environment_id IS NULL OR b.environment_id = %s) "
        "ORDER BY u.display_name",
        (list(roles), project_id, environment_id),
    ).fetchall()
    return [r["display_name"] for r in rows]


def _entry(
    conn: Conn,
    change: dict[str, Any],
    *,
    section: str,
    blocker: str,
    who: list[str],
    next_action: dict[str, str] | None,
    actions: list[dict[str, str]],
) -> dict[str, Any]:
    project = conn.execute("SELECT slug, name FROM projects WHERE id = %s", (change["project_id"],)).fetchone()
    assert project is not None
    env = None
    if change["environment_id"]:
        env = conn.execute("SELECT slug, name FROM environments WHERE id = %s", (change["environment_id"],)).fetchone()
    return {
        "change_id": str(change["id"]),
        "ref": change_ref(change),
        "title": change["title"],
        "state": change["state"],
        "state_label": STATES[change["state"]],
        "project": {"slug": project["slug"], "name": project["name"]},
        "environment": {"slug": env["slug"], "name": env["name"]} if env else None,
        "section": section,
        "blocker": blocker,
        "who_can_act": who,
        "next_action": next_action,
        "actions": actions,
        "updated_at": change["updated_at"].isoformat() if change.get("updated_at") else None,
    }


def build(conn: Conn, principal: Principal, *, now: datetime | None = None) -> dict[str, Any]:
    """Construit les deux listes pour le principal courant (périmètre projet uniquement)."""
    now = now or datetime.now(UTC)
    # Demandes non terminales visibles ; hors périmètre : absentes (pas 403 listées).
    changes = conn.execute(
        "SELECT c.* FROM changes c "
        "WHERE c.state NOT IN ('failed', 'cancelled', 'succeeded') "
        "ORDER BY c.updated_at DESC LIMIT 200"
    ).fetchall()

    mine: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []

    for change in changes:
        if not principal.can_see_project(change["project_id"]):
            continue
        available = workflow.available_actions(conn, principal, "change", change, now)
        mine_actions = [a for a in available if a["action"] in MINE_ACTIONS]
        labeled = [{"action": a["action"], "label": INBOX_LABELS.get(a["action"], a["label"])} for a in mine_actions]
        action_names = {a["action"] for a in labeled}

        # Plan prêt / reprise : l'étape suivante est l'approbation. Le demandeur (edit/cancel)
        # voit « en attente d'approbation », pas « À moi d'agir » pour modifier.
        pending_retry = conn.execute(
            "SELECT 1 FROM retry_requests WHERE change_id = %s AND status = 'pending' LIMIT 1",
            (change["id"],),
        ).fetchone()
        prefer_waiting = (change["state"] == "plan_ready" and "approve" not in action_names) or (
            pending_retry is not None and "approve_retry" not in action_names
        )

        if labeled and not prefer_waiting:
            labeled.sort(key=lambda a: NEXT_PRIORITY.index(a["action"]) if a["action"] in NEXT_PRIORITY else 99)
            next_a = labeled[0]
            roles_for_next = workflow.ROLES.get(next_a["action"], ())
            who = _who_can_act(conn, change["project_id"], change["environment_id"], roles_for_next)
            mine.append(
                _entry(
                    conn,
                    change,
                    section="mine",
                    blocker=_blocker_for_mine(change["state"], next_a["action"]),
                    who=who or [ROLE_LABELS.get(r, r) for r in roles_for_next],
                    next_action=next_a,
                    actions=labeled,
                )
            )
            continue

        hint = WAITING_HINTS.get(change["state"])
        if hint is None and pending_retry is not None:
            hint = ("En attente d'approbation de la reprise", ("approver",))
        if hint is None:
            continue

        blocker, roles = hint
        who = _who_can_act(conn, change["project_id"], change["environment_id"], roles)
        waiting.append(
            _entry(
                conn,
                change,
                section="waiting",
                blocker=blocker,
                who=who or [ROLE_LABELS.get(r, r) for r in roles],
                next_action=None,
                actions=[],
            )
        )

    return {
        "mine": mine,
        "waiting": waiting,
        "notice": "Les actions viennent du workflow et de vos droits actuels ; elles sont revérifiées au clic.",
    }
