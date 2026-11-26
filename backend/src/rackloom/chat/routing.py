"""F48 : routage automatique des modèles selon la tâche.

Choix côté serveur uniquement. Aucun sélecteur pour l'utilisateur standard,
aucun repli cloud silencieux. Si le modèle d'analyse est indisponible, le
formulaire reste utilisable et l'interface reçoit un motif explicite.
"""

from __future__ import annotations

from typing import Any

from ..config import get_settings
from ..db import Conn, Jsonb
from ..errors import ApiError

# Tâches connues → rôle de modèle. Les noms de modèles viennent des réglages locaux.
TASKS = ("draft", "explain", "diagnose")

DEFAULT_ROUTES: dict[str, dict[str, Any]] = {
    "draft": {"role": "draft", "timeout_seconds": 30},
    "explain": {"role": "analysis", "timeout_seconds": 45},
    "diagnose": {"role": "analysis", "timeout_seconds": 60},
}


def _stored(conn: Conn) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM platform_settings WHERE key = 'model_routing'").fetchone()
    return dict(row["value"]) if row else {}


def routing_config(conn: Conn) -> dict[str, Any]:
    """Configuration de routage (sans secret). Modèles locaux uniquement."""
    settings = get_settings()
    stored = _stored(conn)
    models = stored.get("models") or {
        "draft": settings.ollama_model,
        "analysis": settings.ollama_model,
    }
    disabled_roles = set(stored.get("disabled_roles") or [])
    routes = {}
    for task, meta in DEFAULT_ROUTES.items():
        role = meta["role"]
        available = role not in disabled_roles
        routes[task] = {
            "task": task,
            "role": role,
            "model": models.get(role) or settings.ollama_model,
            "timeout_seconds": meta["timeout_seconds"],
            "available": available,
            "reason": None if available else f"modèle « {role} » indisponible",
        }
    return {
        "routes": routes,
        "models": models,
        "disabled_roles": sorted(disabled_roles),
        "cloud_fallback": False,
    }


def select_route(conn: Conn, task: str) -> dict[str, Any]:
    if task not in TASKS:
        raise ApiError(422, "invalid_task", f"tâche attendue : {', '.join(TASKS)}", field="/mode")
    cfg = routing_config(conn)
    route = cfg["routes"][task]
    return route


def set_routing(
    conn: Conn,
    *,
    models: dict[str, str] | None = None,
    disabled_roles: list[str] | None = None,
) -> dict[str, Any]:
    """Réservé à l'administrateur. N'accepte aucun endpoint cloud."""
    current = _stored(conn)
    next_models = dict(current.get("models") or {})
    if models is not None:
        for role, name in models.items():
            if role not in ("draft", "analysis"):
                raise ApiError(422, "invalid_role", f"rôle de modèle inconnu : {role}", field="/models")
            if not isinstance(name, str) or not name.strip() or "://" in name:
                raise ApiError(422, "invalid_model", "nom de modèle local attendu", field=f"/models/{role}")
            next_models[role] = name.strip()
    next_disabled = list(current.get("disabled_roles") or [])
    if disabled_roles is not None:
        bad = [r for r in disabled_roles if r not in ("draft", "analysis")]
        if bad:
            raise ApiError(422, "invalid_role", f"rôle inconnu : {bad[0]}", field="/disabled_roles")
        next_disabled = sorted(set(disabled_roles))
    value = {"models": next_models, "disabled_roles": next_disabled, "cloud_fallback": False}
    conn.execute(
        "INSERT INTO platform_settings (key, value) VALUES ('model_routing', %s) ON CONFLICT (key) DO UPDATE "
        "SET value = EXCLUDED.value, updated_at = now()",
        (Jsonb(value),),
    )
    return routing_config(conn)


def analysis_unavailable_notice(task: str) -> str:
    return (
        f"L'analyse ({task}) n'a pas pu être réalisée : le modèle d'analyse est indisponible. "
        "Le formulaire reste utilisable ; les contrôles et l'approbation sont les mêmes. "
        "Aucun repli cloud n'a été tenté."
    )
