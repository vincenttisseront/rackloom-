"""F51 : « Qu'est-ce que je peux demander ? »

Exemples calculés côté serveur à partir des services publiés octroyés et des droits
réels. Indépendants de l'assistant (mêmes exemples si Ollama est désactivé).
« can_submit » ≠ « can_execute » : l'exécution exige toujours plan, approbation et admission.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .. import projects, registry
from ..db import Conn
from ..identity.authz import Principal

NOTICE = (
    "Exemples basés sur vos droits et services octroyés. "
    "Aucune suggestion de production sans droit. "
    "Soumettre une demande n'autorise pas encore l'exécution."
)

EXECUTE_NOTE = "L'exécution exigera un plan, une approbation et l'admission."


def _lifetime_default(lifetime: dict[str, Any]) -> str | None:
    if not lifetime.get("required"):
        return None
    max_days = int(lifetime.get("max_days") or 30)
    days = max(1, min(max_days, 14))
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat()


def _targets(conn: Conn, principal: Principal, project: dict[str, Any]) -> list[dict[str, Any]]:
    """Services + environnements où le principal peut déployer (filtre droits + octrois)."""
    envs = projects.environments(conn, project["id"])
    out: list[dict[str, Any]] = []
    for slug in registry.granted_services(conn, project["id"]):
        tpl = registry.latest_published(conn, slug)
        if tpl is None:
            continue
        contract = tpl["contract"]
        admissible = set(contract["deployment_policy"]["admissible_environment_kinds"]) & set(contract["environments"])
        spec = contract["inputs"]
        labels = spec["size_tier"].get("value_labels", {})
        for env in envs:
            if env["kind"] not in admissible:
                continue
            if not principal.has_role("operator", project["id"], env["id"]):
                continue
            if slug not in registry.granted_services(conn, project["id"], env["id"]):
                continue
            params = contract["environments"][env["kind"]]
            rules = (projects.current_policy(conn, env["id"]) or {}).get("rules", {})
            lifetime = rules.get("lifetime", {})
            size_tiers = [{"value": t, "label": labels.get(t, t)} for t in params["size_tiers"]]
            out.append(
                {
                    "slug": slug,
                    "title": contract["title"],
                    "description": contract.get("description") or "",
                    "quantity_default": spec["quantity"].get("default", 1),
                    "env": env,
                    "size_tiers": size_tiers,
                    "default_size_tier": params["default_size_tier"],
                    "lifetime": {
                        "required": bool(lifetime.get("required")),
                        "max_days": lifetime.get("max_days"),
                    },
                }
            )
    return out


def _example(project: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    env = target["env"]
    tier = target["default_size_tier"]
    tier_label = next((t["label"] for t in target["size_tiers"] if t["value"] == tier), tier)
    inputs: dict[str, Any] = {
        "quantity": target["quantity_default"],
        "size_tier": tier,
    }
    missing: list[str] = []
    lifetime_val = _lifetime_default(target["lifetime"])
    if lifetime_val:
        inputs["lifetime_until"] = lifetime_val
    elif target["lifetime"].get("required"):
        missing.append("/inputs/lifetime_until")

    return {
        "id": f"{project['slug']}-{target['slug']}-{env['slug']}-{tier}",
        "title": f"{target['title']} ({tier_label}) — {env['name']}",
        "description": target["description"],
        "project": {"slug": project["slug"], "name": project["name"]},
        "need": {
            "schema": "rackloom.need/v1",
            "service": {"template": target["slug"]},
            "environment": env["slug"],
            "confirm_production": bool(env["is_production"]),
            "inputs": inputs,
        },
        "missing_fields": missing,
        "can_submit": True,
        "can_execute": False,
        "execute_note": EXECUTE_NOTE,
        "is_production": bool(env["is_production"]),
    }


def build(
    conn: Conn,
    principal: Principal,
    *,
    project_ref: str | None = None,
) -> dict[str, Any]:
    """Construit les exemples pour un projet, ou pour tous les projets où l'opérateur agit."""
    if project_ref is not None:
        plist = [projects.get_project(conn, principal, project_ref)]
    else:
        plist = list(projects.list_projects(conn, principal))

    examples: list[dict[str, Any]] = []
    for project in plist:
        row = projects.get_project(conn, principal, str(project["id"]))
        if not principal.has_role("operator", row["id"]):
            continue
        for target in _targets(conn, principal, row):
            examples.append(_example(row, target))

    return {
        "examples": examples,
        "notice": NOTICE,
        "assistant_independent": True,
    }
