"""F58 : actions courantes depuis la fiche service.

Chaque action est déclarée par le template (ou un défaut sûr), filtrée par l'état du
service et les droits, puis ouvre une demande préremplie — jamais une commande directe.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import UUID

from .. import registry, workflow
from ..changes import service
from ..changes.transitions import EDITABLE
from ..db import Conn
from ..errors import ApiError
from ..identity.authz import Principal, require_role
from ..ids import uuid7

# Actions F58 reconnues (identifiants stables API / workflow).
ACTION_INCREASE_CAPACITY = "increase_capacity"
ACTION_REQUEST_RESTART = "request_restart"

DEFAULT_ACTIONS: list[dict[str, Any]] = [
    {
        "id": ACTION_INCREASE_CAPACITY,
        "label": "Augmenter la capacité",
        "kind": "modify_capacity",
        "requires_status": ["ready", "created_not_ready"],
        "impact": "may_replace",
        "impact_label": "Peut entraîner le remplacement de la ou des VM (bloqué au lot 1).",
    },
    {
        "id": ACTION_REQUEST_RESTART,
        "label": "Demander un redémarrage",
        "kind": "restart",
        "requires_status": ["ready", "created_not_ready"],
        "impact": "interruption",
        "impact_label": "Interruption de service pendant le redémarrage.",
    },
]

SIZE_ORDER = ("test_leger", "standard", "charge_elevee")


def _contract_actions(contract: dict[str, Any]) -> list[dict[str, Any]]:
    raw = contract.get("service_actions")
    if not raw:
        return list(DEFAULT_ACTIONS)
    out: list[dict[str, Any]] = []
    by_id = {a["id"]: a for a in DEFAULT_ACTIONS}
    for item in raw:
        if not isinstance(item, dict) or item.get("id") not in by_id:
            continue
        base = dict(by_id[item["id"]])
        if item.get("label"):
            base["label"] = str(item["label"])[:120]
        if item.get("requires_status"):
            base["requires_status"] = list(item["requires_status"])
        out.append(base)
    return out or list(DEFAULT_ACTIONS)


def catalog_for_deployment(conn: Conn, dep: dict[str, Any]) -> list[dict[str, Any]]:
    """Catalogue brut des actions du template (sans filtrage rôle / état)."""
    tpl = registry.template_by_id(conn, dep["template_version_id"])
    return _contract_actions(tpl["contract"])


def next_size_tier(conn: Conn, dep: dict[str, Any]) -> str | None:
    """Prochain niveau supérieur autorisé dans l'environnement courant."""
    tpl = registry.template_by_id(conn, dep["template_version_id"])
    env = conn.execute("SELECT kind, slug FROM environments WHERE id = %s", (dep["environment_id"],)).fetchone()
    params = (tpl["contract"].get("environments") or {}).get(env["kind"]) or {}
    available = list((params.get("size_tiers") or {}).keys())
    current = None
    for inst in dep.get("instances") or []:
        if inst.get("size_tier"):
            current = inst["size_tier"]
            break
    if current is None:
        current = params.get("default_size_tier")
    if current is None:
        return None
    ordered = [t for t in SIZE_ORDER if t in available]
    if current not in ordered:
        return None
    idx = ordered.index(current)
    if idx + 1 >= len(ordered):
        return None
    return ordered[idx + 1]


def evaluate_service_actions(conn: Conn, dep: dict[str, Any]) -> dict[str, str | None]:
    """Motifs d'indisponibilité par action F58 (None = possible côté état/template)."""
    catalog = catalog_for_deployment(conn, dep)
    status = dep.get("service_status") or "unknown"
    busy = None
    if dep.get("status") != "active":
        busy = "seul un déploiement actif accepte des actions"
    else:
        row = conn.execute(
            "SELECT 1 FROM changes WHERE deployment_id = %s AND state = ANY(%s) LIMIT 1",
            (dep["id"], list(workflow.DEPLOYMENT_BUSY)),
        ).fetchone()
        if row:
            busy = "un changement est déjà en cours sur ce déploiement"
    out: dict[str, str | None] = {}
    for action in catalog:
        aid = action["id"]
        if busy:
            out[aid] = busy
            continue
        if status not in action.get("requires_status", []):
            out[aid] = f"action indisponible pour l'état de service « {status} »"
            continue
        if aid == ACTION_INCREASE_CAPACITY and next_size_tier(conn, dep) is None:
            out[aid] = "aucun niveau supérieur n'est proposé dans cet environnement"
            continue
        out[aid] = None
    return out


def describe_for_card(conn: Conn, principal: Principal, dep: dict[str, Any]) -> list[dict[str, Any]]:
    """Actions affichables sur la fiche (rôle + état), avec impact visible."""
    catalog = {a["id"]: a for a in catalog_for_deployment(conn, dep)}
    available = {a["action"]: a for a in workflow.available_actions(conn, principal, "deployment", dep)}
    out: list[dict[str, Any]] = []
    for aid, meta in catalog.items():
        if aid not in available:
            continue
        entry = {
            "id": aid,
            "action": aid,
            "label": available[aid]["label"],
            "impact": meta.get("impact"),
            "impact_label": meta.get("impact_label"),
            "kind": meta.get("kind"),
        }
        if aid == ACTION_INCREASE_CAPACITY:
            nxt = next_size_tier(conn, dep)
            entry["target_size_tier"] = nxt
            if nxt:
                entry["impact_label"] = (
                    f"Passage au niveau « {nxt} » : remplacement possible des VM "
                    "(affiché avant exécution ; bloqué au lot 1 s'il y a remplacement)."
                )
        out.append(entry)
    return out


def start(
    conn: Conn,
    principal: Principal,
    deployment_id: UUID,
    action_id: str,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Ouvre la demande préremplie correspondant à l'action (revérifie les droits)."""
    dep = service.get_deployment(conn, principal, deployment_id, for_update=True)
    if action_id not in (ACTION_INCREASE_CAPACITY, ACTION_REQUEST_RESTART):
        raise ApiError(422, "unknown_action", f"action « {action_id} » inconnue", field="/action")
    workflow.require_action(conn, principal, "deployment", dep, action_id)
    if action_id == ACTION_INCREASE_CAPACITY:
        return _start_increase_capacity(conn, principal, dep, request_id=request_id)
    return _start_restart(conn, principal, dep, request_id=request_id)


def _start_increase_capacity(
    conn: Conn, principal: Principal, dep: dict[str, Any], *, request_id: str | None
) -> dict[str, Any]:
    require_role(principal, "operator", dep["project_id"], dep["environment_id"])
    nxt = next_size_tier(conn, dep)
    if nxt is None:
        raise ApiError(409, "action_unavailable", "aucun niveau supérieur disponible")
    change = service.create_modify_change(conn, principal, dep["id"], request_id)
    env = conn.execute("SELECT slug FROM environments WHERE id = %s", (dep["environment_id"],)).fetchone()
    lifetime = (date.today() + timedelta(days=14)).isoformat()
    need = {
        "schema": "rackloom.need/v1",
        "service": {"template": dep["template_slug"]},
        "environment": env["slug"],
        "inputs": {
            "quantity": max(1, len(dep.get("instances") or []) or 1),
            "size_tier": nxt,
            "usage": "Augmentation de capacité depuis la fiche service",
            "lifetime_until": lifetime,
        },
        "confirm_production": bool(
            conn.execute("SELECT is_production FROM environments WHERE id = %s", (dep["environment_id"],)).fetchone()[
                "is_production"
            ]
        ),
    }
    service.add_version(
        conn,
        principal,
        change["id"],
        {"need": need, "base_version_no": 0, "source": "api"},
        request_id,
    )
    change = conn.execute("SELECT * FROM changes WHERE id = %s", (change["id"],)).fetchone()
    rev = service.current_revision(conn, change)
    return {
        "action": ACTION_INCREASE_CAPACITY,
        "change_id": str(change["id"]),
        "change": change,
        "revision": rev,
        "target_size_tier": nxt,
        "impact": "may_replace",
        "notice": "Demande préremplie : passez par le plan et l'approbation. Aucune commande directe.",
    }


def _start_restart(conn: Conn, principal: Principal, dep: dict[str, Any], *, request_id: str | None) -> dict[str, Any]:
    """Demande de redémarrage : kind=restart, plan simulé après soumission, job service_restart."""
    require_role(principal, "operator", dep["project_id"], dep["environment_id"])
    busy_states = (service.ACTIVE_STATES | EDITABLE) - {"rejected", "plan_failed"}
    busy = conn.execute(
        "SELECT id FROM changes WHERE deployment_id = %s AND state = ANY(%s)",
        (dep["id"], list(busy_states)),
    ).fetchone()
    if busy:
        raise ApiError(
            409,
            "change_in_progress",
            "un changement est déjà en cours sur ce déploiement",
            details={"change_id": str(busy["id"])},
        )
    names = ", ".join(i["name"] for i in (dep.get("instances") or [])) or "service"
    title = f"Redémarrer {names}"[:120]
    row = conn.execute(
        "INSERT INTO changes (id, kind, deployment_id, project_id, environment_id, title, state, created_by) "
        "VALUES (%s, 'restart', %s, %s, %s, %s, 'draft', %s) RETURNING *",
        (uuid7(), dep["id"], dep["project_id"], dep["environment_id"], title, principal.user_id),
    ).fetchone()
    service._created(conn, principal, row, request_id)
    env = conn.execute("SELECT slug FROM environments WHERE id = %s", (dep["environment_id"],)).fetchone()
    current_tier = next(
        (i.get("size_tier") for i in (dep.get("instances") or []) if i.get("size_tier")),
        "test_leger",
    )
    need = {
        "schema": "rackloom.need/v1",
        "service": {"template": dep["template_slug"]},
        "environment": env["slug"],
        "inputs": {
            "quantity": max(1, len(dep.get("instances") or []) or 1),
            "size_tier": current_tier,
            "usage": "Redémarrage demandé depuis la fiche service",
            "lifetime_until": (date.today() + timedelta(days=14)).isoformat(),
        },
        "confirm_production": bool(
            conn.execute("SELECT is_production FROM environments WHERE id = %s", (dep["environment_id"],)).fetchone()[
                "is_production"
            ]
        ),
    }
    service.add_version(
        conn,
        principal,
        row["id"],
        {"need": need, "base_version_no": 0, "source": "api"},
        request_id,
    )
    change = conn.execute("SELECT * FROM changes WHERE id = %s", (row["id"],)).fetchone()
    return {
        "action": ACTION_REQUEST_RESTART,
        "change_id": str(change["id"]),
        "change": change,
        "impact": "interruption",
        "impact_label": "Interruption de service pendant le redémarrage.",
        "notice": "Demande préremplie : plan et approbation requis. Le redémarrage est simulé (D2).",
    }


def enqueue_restart(conn: Conn, change: dict[str, Any], *, approval_id: UUID | None = None) -> dict[str, Any]:
    from ..executions import queue

    return queue.enqueue(
        conn,
        kind="service_restart",
        idempotency_key=f"service_restart:{change['id']}:{change['current_revision_no']}",
        change=change,
        payload={"approval_id": str(approval_id) if approval_id else None, "simulated": True},
        priority=80,
    )
