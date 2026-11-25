"""Actions permises sur une demande ou un déploiement, calculées côté serveur.

`available_actions` est la seule source : l'UI n'affiche que ces actions, et chaque route
d'action appelle `require_action`, qui réévalue la même fonction au moment de l'appel.
Un droit manquant ou la séparation des rôles renvoie 403 ; une action devenue impossible renvoie 409.
"""


from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from . import resolver
from .changes.transitions import ALLOWED, EDITABLE, STATES
from .db import Conn
from .errors import ApiError
from .identity.authz import Principal
from .projects import freezes

LABELS = {
    "edit": "Modifier le besoin",
    "submit": "Soumettre",
    "cancel": "Annuler la demande",
    "approve": "Approuver",
    "reject": "Refuser",
    "request_retry": "Demander une reprise de la configuration",
    "approve_retry": "Approuver la reprise",
    "reject_retry": "Refuser la reprise",
    "replan": "Nouvelle planification",
    "duplicate": "Dupliquer",
    "cancel_job": "Demander l'annulation",
    "modify": "Modifier ce déploiement",
    "freeze": "Geler l'environnement",
    "unfreeze": "Dégeler l'environnement",
}

# Rôle exigé par action (sur le projet et l'environnement de la ressource).
# Gel : décision D8, provisoire (administrateur de plateforme ou approbateur de l'environnement).
ROLES = {
    "edit": ("operator",), "submit": ("operator",), "cancel": ("operator", "approver"),
    "approve": ("approver",), "reject": ("approver",), "request_retry": ("operator",),
    "approve_retry": ("approver",), "reject_retry": ("approver",), "replan": ("operator",),
    "duplicate": ("operator",), "cancel_job": ("operator", "approver"), "modify": ("operator",),
    "freeze": ("approver", "platform_admin"), "unfreeze": ("approver", "platform_admin"),
}


RUNNING_JOB_STATES = ("queued", "leased", "running")


class Forbidden(str):
    """Motif lié à la personne (séparation des rôles) : refus 403, pas 409."""


class Coded(str):
    """Motif d'indisponibilité (409) portant un code stable plutôt que `action_unavailable`."""

    code: str

    def __new__(cls, text: str, code: str) -> Coded:
        obj = super().__new__(cls, text)
        obj.code = code
        return obj


FROZEN = Coded("environnement gelé : approbation suspendue jusqu'au dégel", "env_frozen")

DEPLOYMENT_BUSY = (EDITABLE | {"validated", "planning", "approved", "executing", "verifying", "uncertain",
                               "reconciling"}) - {"rejected", "plan_failed"}


def _has_role(principal: Principal, action: str, project_id, environment_id) -> bool:
    return any(principal.has_role(role, project_id, environment_id) for role in ROLES[action])


def _change_actions(conn: Conn, principal: Principal, change: dict[str, Any], now: datetime) -> dict[str, str | None]:
    """Renvoie {action: None si possible, sinon motif d'indisponibilité}."""
    state = change["state"]
    out: dict[str, str | None] = {}
    revision = conn.execute(
        "SELECT * FROM request_revisions WHERE change_id = %s AND revision_no = %s",
        (change["id"], change["current_revision_no"]),
    ).fetchone()
    version = conn.execute(
        "SELECT * FROM request_versions WHERE change_id = %s AND version_no = %s",
        (change["id"], change["current_version_no"]),
    ).fetchone()

    out["edit"] = None if state in EDITABLE else f"demande non modifiable dans l'état « {STATES[state]} »"

    if state not in ("draft", "plan_expired"):
        out["submit"] = f"soumission impossible dans l'état « {STATES[state]} »"
    elif revision is None or revision["outcome"] != "resolved":
        out["submit"] = "la révision courante n'est pas résolue"
    elif version is not None and resolver.unconfirmed(version["provenance"]):
        out["submit"] = "des valeurs proposées par l'assistant ne sont pas confirmées"
    else:
        out["submit"] = None

    executing = state in ("executing", "verifying", "uncertain", "reconciling")
    out["cancel"] = (
        None
        if "cancelled" in ALLOWED.get(state, set()) and not executing
        else "annulation de la demande impossible dans cet état"
    )

    plan = conn.execute(
        "SELECT * FROM plans WHERE change_id = %s ORDER BY created_at DESC LIMIT 1", (change["id"],)
    ).fetchone()
    reason = None
    if state != "plan_ready" or plan is None:
        reason = "aucun plan prêt à approuver"
    elif plan["revision_no"] != change["current_revision_no"]:
        reason = "le plan ne correspond plus à la révision courante"
    elif plan["status"] == "blocked_destructive":
        reason = "le plan supprime ou remplace des ressources : approbation impossible au lot 1"
    elif plan["status"] != "ready":
        reason = f"plan dans l'état « {plan['status']} »"
    elif plan["valid_until"] <= now:
        reason = "le plan a expiré"
    elif conn.execute("SELECT 1 FROM approvals WHERE plan_id = %s AND kind = 'plan'", (plan["id"],)).fetchone():
        reason = "ce plan a déjà fait l'objet d'une décision"
    else:
        author = conn.execute(
            "SELECT created_by FROM request_versions WHERE change_id = %s AND version_no = %s",
            (change["id"], plan["version_no"]),
        ).fetchone()
        env = conn.execute("SELECT * FROM environments WHERE id = %s", (change["environment_id"],)).fetchone()
        if author and author["created_by"] == principal.user_id:
            reason = Forbidden("vous ne pouvez pas approuver une version que vous avez rédigée")
        elif env and env["requires_distinct_approver"] and change["created_by"] == principal.user_id:
            reason = Forbidden("l'approbateur doit être distinct du demandeur pour cet environnement")
    out["approve"] = out["reject"] = reason
    frozen = freezes.active(conn, change["environment_id"]) is not None if change["environment_id"] else False
    if frozen and reason is None:
        out["approve"] = FROZEN

    retryable = (
        conn.execute(
            "SELECT count(*) AS n FROM hosts WHERE deployment_id = %s "
            "AND deployment_state IN ('config_failed', 'enrolled', 'wave_blocked')",
            (change["deployment_id"],),
        ).fetchone()["n"]
        if change["deployment_id"]
        else 0
    )
    pending = conn.execute(
        "SELECT * FROM retry_requests WHERE change_id = %s AND status = 'pending' ORDER BY requested_at DESC LIMIT 1",
        (change["id"],),
    ).fetchone()
    if state != "partially_succeeded":
        out["request_retry"] = "reprise possible uniquement après un succès partiel"
    elif not retryable:
        out["request_retry"] = "aucun hôte à reconfigurer"
    elif pending:
        out["request_retry"] = "une reprise attend déjà une approbation"
    else:
        out["request_retry"] = None
    if pending is None:
        out["approve_retry"] = out["reject_retry"] = "aucune reprise en attente"
    elif pending["requested_by"] == principal.user_id:
        out["approve_retry"] = out["reject_retry"] = Forbidden("vous ne pouvez pas approuver votre propre reprise")
    else:
        out["approve_retry"] = out["reject_retry"] = None
        if frozen:
            out["approve_retry"] = FROZEN

    out["replan"] = None if state == "succeeded" else "nouvelle planification possible uniquement après un succès"
    out["duplicate"] = (
        None
        if change["kind"] == "create" and version is not None
        else "seule une demande de création avec un besoin peut être dupliquée"
    )
    running = conn.execute(
        "SELECT 1 FROM jobs WHERE change_id = %s AND state = ANY(%s) AND cancel_requested_at IS NULL LIMIT 1",
        (change["id"], list(RUNNING_JOB_STATES)),
    ).fetchone()
    out["cancel_job"] = None if running and state not in EDITABLE - {"approved"} else "aucun job annulable en cours"
    return out


def _deployment_actions(conn: Conn, deployment: dict[str, Any]) -> dict[str, str | None]:
    if deployment["status"] != "active":
        return {"modify": "seul un déploiement actif peut être modifié"}
    busy = conn.execute("SELECT 1 FROM changes WHERE deployment_id = %s AND state = ANY(%s) LIMIT 1",
                        (deployment["id"], list(DEPLOYMENT_BUSY))).fetchone()
    return {"modify": "un changement est déjà en cours sur ce déploiement" if busy else None}


def _environment_actions(conn: Conn, env: dict[str, Any]) -> dict[str, str | None]:
    frozen = freezes.active(conn, env["id"]) is not None
    return {"freeze": "environnement déjà gelé" if frozen else None,
            "unfreeze": None if frozen else "environnement non gelé"}


def evaluate(conn: Conn, principal: Principal, kind: str, resource: dict[str, Any],
             now: datetime | None = None) -> dict[str, dict[str, Any]]:
    now = now or datetime.now(UTC)
    environment_id = resource.get("environment_id")
    if kind == "change":
        raw = _change_actions(conn, principal, resource, now)
    elif kind == "deployment":
        raw = _deployment_actions(conn, resource)
    elif kind == "environment":
        raw = _environment_actions(conn, resource)
        environment_id = resource["id"]
    else:
        raise ValueError(kind)
    result = {}
    for action, unavailable in raw.items():
        allowed_role = _has_role(principal, action, resource["project_id"], environment_id)
        result[action] = {"role": allowed_role, "reason": unavailable}
    return result


def available_actions(conn: Conn, principal: Principal, kind: str, resource: dict[str, Any],
                      now: datetime | None = None) -> list[dict[str, str]]:
    return [{"action": a, "label": LABELS[a]} for a, v in evaluate(conn, principal, kind, resource, now).items()
            if v["role"] and v["reason"] is None]


def require_action(conn: Conn, principal: Principal, kind: str, resource: dict[str, Any],
                   action: str) -> None:
    verdict = evaluate(conn, principal, kind, resource)[action]
    if not verdict["role"]:
        roles = " ou ".join(ROLES[action])
        raise ApiError(403, "forbidden", f"rôle {roles} requis pour « {LABELS[action]} »")
    if isinstance(verdict["reason"], Forbidden):
        raise ApiError(403, "separation_of_duties", verdict["reason"], details={"action": action})
    if verdict["reason"] is not None:
        raise ApiError(409, getattr(verdict["reason"], "code", "action_unavailable"),
                       f"« {LABELS[action]} » impossible : {verdict['reason']}", details={"action": action})
