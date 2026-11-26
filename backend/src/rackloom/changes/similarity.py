"""F53 : détection des demandes déjà ouvertes (aide, pas idempotence).

Recherche uniquement dans le périmètre consultable. Une demande hors périmètre
n'apparaît ni dans les résultats, ni dans un compteur, ni dans un message (U1).
Aucune fusion automatique.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ..db import Conn
from ..identity.authz import Principal
from ..resolver import validate_need
from .service import change_ref
from .transitions import STATES

# États encore « ouverts » : hors terminaux et hors succès.
ACTIVE_STATES = frozenset(
    {
        "draft",
        "needs_info",
        "resolution_blocked",
        "validated",
        "planning",
        "plan_failed",
        "plan_ready",
        "plan_expired",
        "rejected",
        "approved",
        "blocked",
        "executing",
        "verifying",
        "partially_succeeded",
        "uncertain",
        "reconciling",
    }
)

NOTICE = "Aide à la détection ; deux besoins semblables peuvent être légitimes. Aucune fusion automatique."

EMPTY = {"matches": [], "count": 0, "notice": NOTICE}


def _need_of(conn: Conn, change_id: UUID) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT need FROM request_versions WHERE change_id = %s ORDER BY version_no DESC LIMIT 1",
        (change_id,),
    ).fetchone()
    return row["need"] if row else None


def _score(candidate: dict[str, Any], probe: dict[str, Any]) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0
    c_tpl = (candidate.get("service") or {}).get("template")
    p_tpl = (probe.get("service") or {}).get("template")
    if c_tpl and p_tpl and c_tpl == p_tpl:
        score += 0.5
        reasons.append("même service")
    c_env = candidate.get("environment")
    p_env = probe.get("environment")
    if c_env and p_env and c_env == p_env:
        score += 0.3
        reasons.append("même environnement")
    c_tier = (candidate.get("inputs") or {}).get("size_tier")
    p_tier = (probe.get("inputs") or {}).get("size_tier")
    if c_tier and p_tier and c_tier == p_tier:
        score += 0.15
        reasons.append("même niveau")
    c_qty = (candidate.get("inputs") or {}).get("quantity")
    p_qty = (probe.get("inputs") or {}).get("quantity")
    if c_qty is not None and p_qty is not None and c_qty == p_qty:
        score += 0.05
        reasons.append("même quantité")
    return score, reasons


def find_similar(
    conn: Conn,
    principal: Principal,
    project: dict[str, Any],
    need_raw: Any,
    *,
    exclude_change_id: UUID | None = None,
    min_score: float = 0.5,
    limit: int = 10,
) -> dict[str, Any]:
    """Retourne les demandes actives similaires du projet, dans le périmètre du principal."""
    # Hors projet visible : get_project a déjà levé 404. Ici on filtre encore can_see.
    if not principal.can_see_project(project["id"]):
        return dict(EMPTY)

    probe = validate_need(need_raw, partial=True)
    if not (probe.get("service") or {}).get("template"):
        return dict(EMPTY)

    rows = conn.execute(
        "SELECT * FROM changes WHERE project_id = %s AND state = ANY(%s) ORDER BY updated_at DESC LIMIT 200",
        (project["id"], list(ACTIVE_STATES)),
    ).fetchall()

    scored: list[tuple[float, Any, dict[str, Any]]] = []
    for change in rows:
        if exclude_change_id is not None and change["id"] == exclude_change_id:
            continue
        # U1 : filtre périmètre avant tout calcul / tout titre.
        if not principal.can_see_project(change["project_id"]):
            continue
        need = _need_of(conn, change["id"])
        if not need:
            continue
        score, reasons = _score(need, probe)
        if score < min_score or not reasons:
            continue
        scored.append(
            (
                score,
                change["updated_at"],
                {
                    "change_id": str(change["id"]),
                    "ref": change_ref(change),
                    "title": change["title"],
                    "state": change["state"],
                    "state_label": STATES[change["state"]],
                    "similarity": {"score": round(score, 2), "reasons": reasons},
                },
            )
        )

    # À score égal, les plus récentes d'abord (sinon un CHG-ancien monopolise le top N).
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    matches = [m for _, _, m in scored[:limit]]
    return {"matches": matches, "count": len(matches), "notice": NOTICE}


def empty_payload() -> dict[str, Any]:
    """Réponse bit-identique pour absence de match (tests U1)."""
    return dict(EMPTY)
