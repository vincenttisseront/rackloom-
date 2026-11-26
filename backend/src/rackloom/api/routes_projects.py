"""Projets, environnements, services déployables et listes par projet."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Request

from .. import hosts as hosts_mod
from .. import projects, registry
from ..changes import service
from ..errors import ApiError
from ..identity.authz import Principal
from ..projects import freezes
from .deps import csrf_guard, idempotent, unit_of_work
from .views import change_summary, change_view, deployment_summary, environment_view, freeze_view

router = APIRouter(tags=["projets"], dependencies=[Depends(csrf_guard)])


def _env_view(env: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(env["id"]), "slug": env["slug"], "name": env["name"], "kind": env["kind"],
            "is_production": env["is_production"]}


@router.get("/projects")
def list_projects(request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        return [{"id": str(p["id"]), "slug": p["slug"], "name": p["name"], "code": p["code"], "roles": p["roles"],
                 "referent": projects.referent_name(u.conn, p)} for p in projects.list_projects(u.conn, u.p)]


@router.get("/projects/{project}/environments")
def list_environments(project: str, request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        p = projects.get_project(u.conn, u.p, project)
        out = []
        for env in projects.environments(u.conn, p["id"]):
            pol = projects.current_policy(u.conn, env["id"])
            out.append({**environment_view(u.conn, u.p, env), "policy_version": pol["version"] if pol else None})
        return out


def services_for(conn, principal: Principal, project: dict[str, Any]) -> list[dict[str, Any]]:
    """Services déployables : contrat d'entrées et environnements où l'utilisateur peut déployer."""
    envs = projects.environments(conn, project["id"])
    out = []
    for slug in registry.granted_services(conn, project["id"]):
        guide = registry.latest_published(conn, slug)
        if guide is None:
            continue
        promoted = registry.promoted_kinds(conn, slug)
        guide_contract = guide["contract"]
        admissible = (
            set(guide_contract["deployment_policy"]["admissible_environment_kinds"])
            & set(guide_contract["environments"])
            & promoted
        )
        targets = []
        display_version = None
        for env in envs:
            if env["kind"] not in admissible or not principal.has_role("operator", project["id"], env["id"]):
                continue
            if slug not in registry.granted_services(conn, project["id"], env["id"]):
                continue
            tpl = registry.latest_promoted(conn, slug, env["kind"])
            if tpl is None:
                continue
            if display_version is None or registry.semver_key(tpl["version"]) > registry.semver_key(display_version):
                display_version = tpl["version"]
            contract = tpl["contract"]
            spec = contract["inputs"]
            params = contract["environments"][env["kind"]]
            rules = (projects.current_policy(conn, env["id"]) or {}).get("rules", {})
            lifetime = rules.get("lifetime", {})
            labels = spec["size_tier"].get("value_labels", {})
            targets.append({**_env_view(env), "freeze": freeze_view(freezes.active(conn, env["id"])),
                            "template_version": tpl["version"],
                            "size_tiers": [{"value": t, "label": labels.get(t, t)} for t in params["size_tiers"]],
                            "default_size_tier": params["default_size_tier"],
                            "lifetime": {"required": bool(lifetime.get("required")),
                                         "max_days": lifetime.get("max_days")}})
        if not targets:
            continue
        contract = registry.latest_promoted(conn, slug, targets[0]["kind"])["contract"]
        spec = contract["inputs"]
        out.append({
            "slug": slug, "version": display_version or contract["version"], "title": contract["title"],
            "description": contract["description"],
            "inputs": {"quantity": {k: spec["quantity"][k] for k in ("min", "max", "default", "label")},
                       "size_tier": {"label": spec["size_tier"]["label"]},
                       "usage": {"label": spec["usage"]["label"], "max_length": spec["usage"].get("max_length", 80)},
                       "lifetime_until": {"label": spec["lifetime_until"]["label"]}},
            "environments": [t for t in targets if not t["is_production"]],
            "production": [t for t in targets if t["is_production"]],
            "production_note": "sur demande explicite, avec son circuit d'approbation",
        })
    return out


@router.get("/projects/{project}/services")
def list_services(project: str, request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        p = projects.get_project(u.conn, u.p, project)
        return services_for(u.conn, u.p, p)


@router.get("/projects/{project}/examples")
def list_examples(project: str, request: Request) -> dict[str, Any]:
    """F51 : exemples adaptés au projet, aux templates publiés et aux droits."""
    from .. import examples

    with unit_of_work(request) as u:
        projects.get_project(u.conn, u.p, project)  # 404 hors périmètre
        return examples.build(u.conn, u.p, project_ref=project)


@router.post("/projects/{project}/comparisons")
def compare_choices(project: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """F52 : comparateur de choix métier (lecture seule)."""
    from ..resolver import compare as compare_mod

    extra = set(body) - {"need"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé",
                       field=f"/{sorted(extra)[0]}")
    with unit_of_work(request) as u:
        p = projects.get_project(u.conn, u.p, project)
        return compare_mod.compare(u.conn, u.p, p, body.get("need"))


@router.post("/projects/{project}/changes/similar")
def similar_changes(project: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """F53 : demandes actives similaires dans le périmètre consultable (pas de fusion)."""
    from uuid import UUID

    from ..changes import similarity

    extra = set(body) - {"need", "exclude_change_id"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé",
                       field=f"/{sorted(extra)[0]}")
    with unit_of_work(request) as u:
        p = projects.get_project(u.conn, u.p, project)
        exclude = None
        if body.get("exclude_change_id"):
            try:
                exclude = UUID(str(body["exclude_change_id"]))
            except ValueError as exc:
                raise ApiError(422, "invalid_id", "identifiant invalide",
                               field="/exclude_change_id") from exc
        return similarity.find_similar(
            u.conn, u.p, p, body.get("need"), exclude_change_id=exclude
        )


@router.get("/projects/{project}/changes")
def list_changes(project: str, request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        p = projects.get_project(u.conn, u.p, project)
        rows = u.conn.execute("SELECT * FROM changes WHERE project_id = %s ORDER BY seq DESC LIMIT 200",
                              (p["id"],)).fetchall()
        return [change_summary(u.conn, c) for c in rows]


@router.post("/projects/{project}/changes")
def create_change(project: str, request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    with unit_of_work(request, write=True) as u:
        p = projects.get_project(u.conn, u.p, project)

        def act() -> dict[str, Any]:
            extra = set(body) - {"title"}
            if extra:
                raise ApiError(422, "field_not_allowed", f"champ « {sorted(extra)[0]} » refusé",
                               field=f"/{sorted(extra)[0]}")
            change = service.create_change(u.conn, u.p, p, str(body.get("title") or ""), u.request_id)
            return change_view(u.conn, u.p, change)

        return idempotent(u, request, body, act)


@router.get("/projects/{project}/deployments")
def list_deployments(project: str, request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        p = projects.get_project(u.conn, u.p, project)
        rows = u.conn.execute("SELECT * FROM deployments WHERE project_id = %s AND status <> 'abandoned' "
                              "ORDER BY created_at DESC", (p["id"],)).fetchall()
        return [deployment_summary(u.conn, d) for d in rows]


@router.get("/projects/{project}/hosts")
def list_hosts(project: str, request: Request) -> list[dict[str, Any]]:
    with unit_of_work(request) as u:
        p = projects.get_project(u.conn, u.p, project)
        rows = u.conn.execute("SELECT * FROM hosts WHERE project_id = %s ORDER BY name", (p["id"],)).fetchall()
        return [{"id": str(h["id"]), "name": h["name"], "deployment_id": str(h["deployment_id"]),
                 "deployment_state": h["deployment_state"], "health": hosts_mod.health(u.conn, h)} for h in rows]
