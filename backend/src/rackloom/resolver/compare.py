"""F52 : comparateur de choix métier.

Compare au plus trois options autorisées qui changent vraiment le résultat visible.
Aucun coût ni capacité inventé : critère omis si la donnée n'est pas vérifiée.
Un seul choix valable → mode « auto », sans question technique.
"""

from __future__ import annotations

from typing import Any

from .. import projects, registry
from ..db import Conn
from ..errors import ApiError
from ..identity.authz import Principal
from . import validate_need

# Critères affichés seulement si une source vérifiée existe (F05/F29/F32 pas encore livrés).
VERIFIED_CRITERIA: frozenset[str] = frozenset()  # coût, capacité, dispo : omis jusqu'à données vérifiées
MAX_OPTIONS = 3


def _service_options(
    conn: Conn,
    principal: Principal,
    project: dict[str, Any],
) -> list[dict[str, Any]]:
    """Services publiés octroyés où l'opérateur peut déployer au moins un environnement non-prod."""
    out: list[dict[str, Any]] = []
    for slug in registry.granted_services(conn, project["id"]):
        tpl = registry.latest_published(conn, slug)
        if tpl is None:
            continue
        contract = tpl["contract"]
        # Au moins un env non-prod admissible + droit + octroi.
        ok = False
        for env in projects.environments(conn, project["id"]):
            if env["is_production"]:
                continue
            if env["kind"] not in contract["deployment_policy"]["admissible_environment_kinds"]:
                continue
            if not principal.has_role("operator", project["id"], env["id"]):
                continue
            if slug not in registry.granted_services(conn, project["id"], env["id"]):
                continue
            ok = True
            break
        if not ok:
            continue
        out.append(
            {
                "key": f"service:{slug}",
                "label": contract["title"],
                "dimension": "service",
                "need_patch": [{"path": "/service/template", "value": slug}],
                "summary_headline": contract.get("description") or contract["title"],
                "is_production": False,
                "default": slug == "ubuntu-web",
            }
        )
    return out


def _env_options(
    conn: Conn,
    principal: Principal,
    project: dict[str, Any],
    template_slug: str,
    *,
    confirm_production: bool,
) -> list[dict[str, Any]]:
    tpl = registry.latest_published(conn, template_slug)
    if tpl is None:
        return []
    contract = tpl["contract"]
    admissible = set(contract["deployment_policy"]["admissible_environment_kinds"]) & set(contract["environments"])
    out: list[dict[str, Any]] = []
    for env in projects.environments(conn, project["id"]):
        if env["kind"] not in admissible:
            continue
        if env["is_production"] and not confirm_production:
            continue
        if not principal.has_role("operator", project["id"], env["id"]):
            continue
        if template_slug not in registry.granted_services(conn, project["id"], env["id"]):
            continue
        out.append(
            {
                "key": f"environment:{env['slug']}",
                "label": env["name"],
                "dimension": "environment",
                "need_patch": [{"path": "/environment", "value": env["slug"]}],
                "summary_headline": f"Environnement « {env['name']} »",
                "is_production": bool(env["is_production"]),
                "default": False,
            }
        )
    return out


def _tier_options(
    conn: Conn,
    project: dict[str, Any],
    template_slug: str,
    env_slug: str,
) -> list[dict[str, Any]]:
    tpl = registry.latest_published(conn, template_slug)
    if tpl is None:
        return []
    contract = tpl["contract"]
    env_row = next(
        (e for e in projects.environments(conn, project["id"]) if e["slug"] == env_slug),
        None,
    )
    if env_row is None or env_row["kind"] not in contract["environments"]:
        return []
    params = contract["environments"][env_row["kind"]]
    labels = contract["inputs"]["size_tier"].get("value_labels", {})
    default = params["default_size_tier"]
    out: list[dict[str, Any]] = []
    for tier in params["size_tiers"]:
        label = labels.get(tier, tier)
        out.append(
            {
                "key": f"size_tier:{tier}",
                "label": label,
                "dimension": "size_tier",
                "need_patch": [{"path": "/inputs/size_tier", "value": tier}],
                "summary_headline": f"Niveau « {label} »",
                "is_production": False,
                "default": tier == default,
            }
        )
    return out


def _attach_criteria(option: dict[str, Any]) -> dict[str, Any]:
    """N'ajoute que des critères avec données vérifiées ; le reste est omis (U9)."""
    criteria: dict[str, Any] = {}
    # Pas de F05/F29/F32 : aucun critère chiffré inventé.
    omitted = ["cost", "capacity", "availability", "duration"]
    return {**option, "criteria": criteria, "omitted_criteria": omitted}


def compare(
    conn: Conn,
    principal: Principal,
    project: dict[str, Any],
    need_raw: Any,
) -> dict[str, Any]:
    """Compare les options métier autorisées pour un besoin partiel."""
    raw = need_raw if isinstance(need_raw, dict) else {}
    template = (raw.get("service") or {}).get("template") if isinstance(raw.get("service"), dict) else None
    options: list[dict[str, Any]] = []
    dimension: str | None = None

    # Ordre : service (si plusieurs profils) → environnement → niveau.
    if not isinstance(template, str) or not template:
        options = _service_options(conn, principal, project)
        dimension = "service"
        need = {"inputs": {}, "environment": None, "confirm_production": False}
    else:
        need = validate_need(raw, partial=True)
        template = (need.get("service") or {}).get("template")
        env = need.get("environment")
        confirm = bool(need.get("confirm_production"))
        inputs = need.get("inputs") or {}
        if template not in registry.granted_services(conn, project["id"]):
            raise ApiError(404, "not_found", "service introuvable")
        if not env:
            options = _env_options(conn, principal, project, template, confirm_production=confirm)
            dimension = "environment"
        elif "size_tier" not in inputs:
            options = _tier_options(conn, project, template, env)
            dimension = "size_tier"
        else:
            return {
                "mode": "auto",
                "dimension": None,
                "options": [],
                "recommendation": None,
                "omitted_criteria": ["cost", "capacity", "availability", "duration"],
                "notice": "Les choix métier sont déjà fixés ; aucune comparaison.",
            }

    options = [_attach_criteria(o) for o in options[:MAX_OPTIONS]]

    if len(options) <= 1:
        chosen = options[0] if options else None
        return {
            "mode": "auto",
            "dimension": dimension,
            "options": options,
            "recommendation": {
                "key": chosen["key"],
                "rationale": "Une seule option autorisée ; sélectionnée sans question technique.",
            }
            if chosen
            else None,
            "omitted_criteria": ["cost", "capacity", "availability", "duration"],
            "notice": "Une seule option valable : pas de comparateur affiché.",
        }

    preferred = next((o for o in options if o.get("default")), options[0])
    if dimension == "environment":
        preferred = next((o for o in options if not o.get("is_production")), options[0])
        rationale = (
            f"« {preferred['label']} » est l'environnement non productif autorisé par défaut ; "
            "la production n'est jamais proposée sans confirmation explicite."
        )
    elif dimension == "service":
        preferred = next((o for o in options if o.get("default")), options[0])
        rationale = (
            f"« {preferred['label']} » est le profil de base ; "
            "un autre profil n'est proposé que s'il est publié et octroyé. "
            "Aucun coût ni capacité non vérifié n'est affiché."
        )
    else:
        rationale = (
            f"« {preferred['label']} » est le niveau par défaut du template pour cet environnement. "
            "Aucun coût ni capacité non vérifié n'est affiché."
        )

    return {
        "mode": "compare",
        "dimension": dimension,
        "options": options,
        "recommendation": {"key": preferred["key"], "rationale": rationale},
        "omitted_criteria": ["cost", "capacity", "availability", "duration"],
        "notice": (
            "Comparaison limitée aux options autorisées. "
            "Capacité, disponibilité et coût omis faute de données vérifiées."
        ),
    }
