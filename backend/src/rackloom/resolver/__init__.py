"""Résolveur central : du besoin exprimé aux paramètres techniques, côté serveur uniquement.

WebUI, chat, API et automatisations passent par `parse_need` puis `resolve`. Le demandeur
ne fournit que le besoin (catégorie A) ; tout le reste (catégorie B) est déduit du template
publié, de l'inventaire octroyé et des politiques, avec sa provenance. Aucun champ technique
fourni par l'utilisateur n'est accepté, même s'il vaut le défaut.

Règles :
1. contraintes obligatoires (plateforme, projet, environnement) d'abord ;
2. parmi les choix restants, défaut du template ;
3. départage explicite et déterministe quand plusieurs placements conviennent ;
4. aucun choix possible : résolution bloquée avec une explication, jamais de bascule ;
5. jamais la production par défaut.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import catalog, inventory, policy
from ..canonical import digest
from ..config import get_settings
from ..errors import ApiError

NEED_SCHEMA = "rackloom.need/v1"

NEED_FIELDS = ("schema", "template", "usage", "size", "quantity", "lifetime_days", "environment_id",
               "description")

# Champs résolus par Rackloom : les fournir dans une demande est refusé, même avec la bonne valeur.
LOCKED_FIELDS = {
    "template_version", "image", "image_ref", "template_ref", "vsphere_template", "cluster", "resource_pool",
    "folder", "datastore", "placement", "host", "network", "network_ref", "vlan", "portgroup", "ip", "ip_address",
    "dns", "hostname", "name", "names", "vm_name", "vms", "naming", "cpu", "vcpu", "cpus", "memory",
    "memory_mb", "ram", "disk", "disk_gb", "storage", "packages", "package_version", "software", "components",
    "config", "roles", "role", "playbook", "runner", "zone", "state", "state_backend", "backend", "module",
    "provider", "providers", "hcl", "terraform", "ansible", "identity", "credentials", "secret", "secrets",
    "backup", "quota", "approval", "approver", "policy", "verification", "health_checks", "project_id",
}

PROTECTED_POINTERS = ("/environment_id", "/usage", "/template")
VALID_SOURCES = ("user", "suggestion")


class Need(BaseModel):
    """Besoin fonctionnel. `extra="forbid"` : tout autre champ est refusé."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["rackloom.need/v1"] = Field(alias="schema")
    template: str = Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")
    usage: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,30}$")
    size: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{1,30}$")
    quantity: int | None = Field(default=None, ge=1, le=10, strict=True)
    lifetime_days: int | None = Field(default=None, ge=1, le=365, strict=True)
    environment_id: UUID | None = None
    description: str = Field(default="", max_length=500)

    def as_content(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        if not data.get("description"):
            data.pop("description", None)
        return data


def parse_need(raw: Any) -> Need:
    """Valide la forme du besoin et refuse explicitement les champs verrouillés."""
    if not isinstance(raw, dict):
        raise ApiError(422, "invalid_need", "le besoin doit être un objet JSON", field="/")
    for key in raw:
        if not isinstance(key, str):
            raise ApiError(422, "unknown_field", "clé non textuelle", field="/")
        if key in NEED_FIELDS:
            continue
        lowered = key.lower()
        if lowered in LOCKED_FIELDS or lowered.startswith("ansible_") or lowered.startswith("tf_"):
            raise ApiError(
                422, "locked_field",
                f"« {key} » est résolu par Rackloom à partir du template publié et des politiques ; "
                "il ne peut pas être fourni dans une demande.",
                field=f"/{key}",
            )
        raise ApiError(422, "unknown_field", f"champ « {key} » inconnu du schéma de demande", field=f"/{key}")
    if isinstance(raw.get("description"), str) and re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]",
                                                               raw["description"]):
        raise ApiError(422, "invalid_text", "caractères de contrôle refusés", field="/description")
    try:
        return Need.model_validate(raw)
    except ValidationError as exc:
        err = exc.errors()[0]
        pointer = "/" + "/".join(str(p) for p in err["loc"]).replace("schema_", "schema")
        raise ApiError(422, "invalid_need", err["msg"], field=pointer) from exc


def parse_provenance(raw: Any, need: Need) -> dict[str, dict[str, Any]]:
    """Provenance des valeurs du besoin : `user` ou `suggestion` (assistant, non confirmée)."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ApiError(422, "invalid_provenance", "provenance : objet attendu", field="/provenance")
    content = need.as_content()
    result: dict[str, dict[str, Any]] = {}
    for pointer, entry in raw.items():
        key = pointer.lstrip("/")
        if key not in NEED_FIELDS or key == "schema":
            raise ApiError(422, "invalid_provenance", f"pointeur inconnu {pointer}", field="/provenance")
        if not isinstance(entry, dict) or entry.get("source") not in VALID_SOURCES:
            raise ApiError(422, "invalid_provenance", "source attendue : user ou suggestion",
                           field=f"/provenance{pointer}")
        if key in content:
            result[f"/{key}"] = {"source": entry["source"]}
    for key in content:
        if key != "schema":
            result.setdefault(f"/{key}", {"source": "user"})
    return result


def unconfirmed_suggestions(provenance: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(p for p, e in provenance.items() if e.get("source") == "suggestion")


# --- Résolution -------------------------------------------------------------------------

def _block(code: str, message: str, hint: str | None = None, field: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"code": code, "message": message}
    if hint:
        entry["hint"] = hint
    if field:
        entry["field"] = field
    return entry


def _sorted(items: list[dict[str, Any]], tie_break: list[str]) -> list[dict[str, Any]]:
    result = list(items)
    for rule in reversed(tie_break):
        if rule == "name_asc":
            result.sort(key=lambda i: i["name"])
        elif rule == "free_gb_desc":
            result.sort(key=lambda i: -int(i["attributes"].get("free_gb", 0)))
        elif rule == "free_ips_desc":
            result.sort(key=lambda i: -int(i["attributes"].get("free_ips", 0)))
        else:
            raise ValueError(f"règle de départage inconnue : {rule}")
    return result


def _ref(item: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"stable_id": item["stable_id"], "name": item["name"], **extra}


def _used_names(conn: psycopg.Connection, project_id: UUID, change_id: UUID | None) -> set[str]:
    names = {r["name"] for r in conn.execute("SELECT name FROM hosts WHERE project_id = %s",
                                             (project_id,)).fetchall()}
    rows = conn.execute(
        """
        SELECT rv.resolution FROM changes c
        JOIN request_versions rv ON rv.change_id = c.id AND rv.version_no = c.current_version_no
        WHERE c.project_id = %s AND c.id IS DISTINCT FROM %s
          AND c.state NOT IN ('cancelled', 'rejected', 'failed', 'plan_failed')
        """,
        (project_id, change_id),
    ).fetchall()
    for row in rows:
        for vm in (row["resolution"] or {}).get("vms", []):
            names.add(vm["name"])
    return names


def resolve(conn: psycopg.Connection, *, project: dict[str, Any], need: Need,
            provenance: dict[str, dict[str, Any]], change_id: UUID | None = None) -> dict[str, Any]:
    settings = get_settings()
    blocked: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    prov: dict[str, dict[str, Any]] = {}
    out: dict[str, Any] = {"schema": "rackloom.resolution/v1", "policy": policy.active()}

    # Template publié et proposé au projet.
    enabled = conn.execute(
        "SELECT 1 FROM project_templates WHERE project_id = %s AND template_slug = %s",
        (project["id"], need.template),
    ).fetchone()
    tpl_row = catalog.latest_template(conn, need.template) if enabled else None
    if tpl_row is None:
        out.update(status="blocked", blocked=[_block(
            "template_unavailable", "Ce service n'est pas proposé pour ce projet.",
            "Contactez l'administrateur du projet pour l'ajouter au catalogue du projet.", "/template")],
            questions=[], provenance={})
        return out
    tpl = tpl_row["manifest"]
    out["template"] = {"slug": tpl["slug"], "version": tpl["version"], "digest": tpl_row["digest"],
                       "title": tpl["title"]}
    prov["/template_version"] = {"source": "template_latest_published", "detail": tpl["version"]}

    inputs = tpl["inputs"]
    usage = inputs["usages"].get(need.usage)
    if usage is None:
        raise ApiError(422, "invalid_usage", f"usage « {need.usage} » non prévu par ce service",
                       field="/usage", details={"allowed": sorted(inputs["usages"])})
    out["usage"] = {"key": need.usage, "label": usage["label"]}

    size_key = need.size or inputs["default_size"]
    if size_key not in inputs["sizes"]:
        raise ApiError(422, "invalid_size", f"taille « {size_key} » non prévue par ce service", field="/size",
                       details={"allowed": sorted(inputs["sizes"])})
    size = inputs["sizes"][size_key]
    out["size"] = {"key": size_key, **size}
    prov["/size"] = provenance.get("/size", {"source": "template_default"}) if need.size else \
        {"source": "template_default"}

    q = inputs["quantity"]
    quantity = need.quantity if need.quantity is not None else q["default"]
    if not (q["min"] <= quantity <= q["max"]):
        raise ApiError(422, "invalid_quantity", f"quantité entre {q['min']} et {q['max']} pour ce service",
                       field="/quantity")
    out["quantity"] = quantity
    prov["/quantity"] = provenance.get("/quantity", {"source": "user"}) if need.quantity is not None else \
        {"source": "template_default"}

    lifetime = usage.get("lifetime_days")
    if lifetime:
        days = need.lifetime_days or lifetime["default"]
        if days > lifetime["max"]:
            raise ApiError(422, "invalid_lifetime", f"durée maximale : {lifetime['max']} jours",
                           field="/lifetime_days")
        out["lifetime"] = {"days": days, "applied": False}
        prov["/lifetime"] = {"source": "user" if need.lifetime_days else "template_default"}
    elif need.lifetime_days:
        raise ApiError(422, "invalid_lifetime", "pas de durée de vie pour cet usage", field="/lifetime_days")

    # Environnement : contraintes d'abord, jamais la production par défaut.
    envs = conn.execute("SELECT * FROM environments WHERE project_id = %s ORDER BY slug",
                        (project["id"],)).fetchall()
    allowed = [e for e in envs if e["kind"] in usage["environment_kinds"]]
    env = None
    if need.environment_id is not None:
        env = next((e for e in envs if e["id"] == need.environment_id), None)
        if env is None:
            raise ApiError(422, "invalid_environment", "environnement inconnu pour ce projet",
                           field="/environment_id")
        if env not in allowed:
            blocked.append(_block(
                "environment_not_allowed",
                f"L'environnement « {env['name']} » n'accepte pas l'usage « {usage['label']} ».",
                field="/environment_id"))
            env = None
        else:
            prov["/environment"] = {"source": "user"}
    elif usage.get("explicit_environment_required"):
        questions.append({
            "field": "/environment_id",
            "text": f"Dans quel environnement de {usage['label'].lower()} déployer ? "
                    "Ce choix doit être explicite.",
            "options": [{"id": str(e["id"]), "label": e["name"]} for e in allowed],
        })
    else:
        candidates = [e for e in allowed if e["kind"] != "prod"]
        if not candidates:
            blocked.append(_block(
                "no_environment",
                f"Aucun environnement de ce projet n'accepte l'usage « {usage['label']} ».",
                "Contactez l'administrateur du projet."))
        elif len(candidates) == 1:
            env = candidates[0]
            prov["/environment"] = {"source": "single_option",
                                    "detail": f"seul environnement autorisé pour l'usage {need.usage}"}
        else:
            questions.append({
                "field": "/environment_id", "text": "Dans quel environnement déployer ?",
                "options": [{"id": str(e["id"]), "label": e["name"]} for e in candidates],
            })

    fresh = inventory.freshness(conn, settings.inventory_max_age_hours)
    out["inventory"] = fresh
    if fresh["stale"]:
        blocked.append(_block(
            "inventory_stale",
            "Les données d'inventaire sont absentes ou trop anciennes ; la résolution est suspendue.",
            "Une nouvelle collecte de l'inventaire est nécessaire (administrateur)."))

    if env is not None:
        out["environment"] = {"id": str(env["id"]), "slug": env["slug"], "name": env["name"], "kind": env["kind"]}
        out["approval"] = {
            "required": True,
            "distinct_approver": bool(env["requires_distinct_approver"]) or env["kind"] == "prod",
            "approver_role": "approver",
        }
        prov["/approval"] = {"source": "environment_policy"}
        granted = inventory.granted_items(conn, project["id"], env["id"]) if not fresh["stale"] else []
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for item in granted:
            by_kind.setdefault(item["kind"], []).append(item)
        where = f"dans l'environnement « {env['name']} »"
        admin_hint = f"Contactez l'administrateur du projet « {project['name']} »."

        img_rule = tpl["creation"]["image"]
        images = [i for i in by_kind.get("image", [])
                  if set(img_rule["tags"]) <= set(i["attributes"].get("tags", []))]
        if not images and not fresh["stale"]:
            blocked.append(_block("no_image", f"Aucune image système compatible n'est autorisée {where}.",
                                  admin_hint))
        net_rule = tpl["system"]["network"]
        networks = [i for i in by_kind.get("network", [])
                    if set(net_rule["tags"]) <= set(i["attributes"].get("tags", []))]
        if not networks and not fresh["stale"]:
            blocked.append(_block("no_network", f"Aucun réseau n'est disponible pour ce profil {where}.",
                                  admin_hint))
        clusters = by_kind.get("cluster", [])
        ds_rule = tpl["resolution"]["datastore"]
        datastores = [i for i in by_kind.get("datastore", [])
                      if int(i["attributes"].get("free_gb", 0)) >= int(ds_rule.get("min_free_gb", 0))]
        if (not clusters or not datastores) and not fresh["stale"]:
            blocked.append(_block("no_capacity", f"Aucune capacité d'hébergement n'est autorisée {where}.",
                                  admin_hint))
        if images and networks and clusters and datastores:
            image = _sorted(images, img_rule["tie_break"])[0]
            network = _sorted(networks, net_rule["tie_break"])[0]
            cluster = _sorted(clusters, tpl["resolution"]["cluster"]["tie_break"])[0]
            datastore = _sorted(datastores, ds_rule["tie_break"])[0]
            folders = _sorted(by_kind.get("folder", []), ["name_asc"])
            out["image"] = _ref(image, guest_os=image["attributes"].get("guest_os"))
            out["network"] = _ref(network, vlan=network["attributes"].get("vlan"))
            out["placement"] = {
                "cluster": _ref(cluster),
                "datastore": _ref(datastore, free_gb=datastore["attributes"].get("free_gb")),
                "folder": _ref(folders[0]) if folders else None,
            }
            out["addressing"] = {
                "ip": "attribuée à la création (simulé : un nœud de labo est associé)",
                "dns": "non géré au lot 1",
            }
            rule_note = "filtre du template puis départage {}"
            prov["/image"] = {"source": "inventory_grant",
                              "detail": rule_note.format(",".join(img_rule["tie_break"]))}
            prov["/network"] = {"source": "inventory_grant",
                                "detail": rule_note.format(",".join(net_rule["tie_break"]))}
            prov["/placement/cluster"] = {"source": "inventory_grant", "detail": "départage name_asc"}
            prov["/placement/datastore"] = {
                "source": "inventory_grant",
                "detail": f"espace libre ≥ {ds_rule.get('min_free_gb', 0)} Go, départage "
                          + ",".join(ds_rule["tie_break"])}
            used = _used_names(conn, project["id"], change_id)
            vms, index = [], 1
            while len(vms) < quantity:
                name = tpl["resolution"]["naming"].format(project=project["slug"], environment=env["slug"],
                                                          index=index)
                index += 1
                if name in used:
                    continue
                vms.append({"name": name, "cpu": size["cpu"], "memory_mb": size["memory_mb"],
                            "disk_gb": size["disk_gb"]})
            out["vms"] = vms
            prov["/vms"] = {"source": "template_naming", "detail": tpl["resolution"]["naming"]}

    components = []
    for comp in catalog.components_of(conn, tpl):
        manifest = comp["item"]["manifest"]
        config = {k: v.get("default") for k, v in manifest["parameters"].items()}
        config.update(comp["ref"].get("config", {}))
        components.append({"name": manifest["name"], "version": manifest["version"],
                           "digest": comp["item"]["digest"], "title": manifest["title"], "config": config,
                           "effects": manifest["effects"], "evidence": manifest.get("evidence")})
    out["components"] = components
    prov["/components"] = {"source": "template", "detail": f"{tpl['slug']}@{tpl['version']}"}

    tf = tpl["creation"]["terraform"]
    tf_item = catalog.get_item(conn, "tf_module", tf["module"], tf["version"])
    out["terraform"] = {"module": tf["module"], "version": tf["version"], "digest": tf_item["digest"],
                        "lockfile_sha256": tf_item["manifest"]["lockfile_sha256"]}
    roles = []
    for comp in catalog.components_of(conn, tpl):
        role = comp["item"]["manifest"]["implementation"]["ansible_role"]
        role_item = catalog.get_item(conn, "ansible_role", role["name"], role["version"])
        roles.append({"name": role["name"], "version": role["version"], "digest": role_item["digest"]})
    playbooks = {}
    for key in ("configure", "collect"):
        ref = tpl["ansible"][key]
        item = catalog.get_item(conn, "playbook", ref["playbook"], ref["version"])
        playbooks[key] = {"playbook": ref["playbook"], "version": ref["version"], "digest": item["digest"]}
    out["ansible"] = {"roles": roles, **playbooks}
    out["runner"] = {"zone": "local", "terraform": {"network": "aucun"}, "ansible": {"network": "rn-lab"}}
    out["state_backend"] = {"kind": "local_demo", "label": "state de démo local, un dossier par stack"}
    out["identities"] = {
        "terraform": "connecteur terraform-cli (aucun identifiant vSphere au lot 1)",
        "configuration": "rackloom-config (élévation requise)",
        "collecte": "rackloom-collect (sans élévation)",
    }
    out["verification"] = catalog.health_checks(conn, tpl)
    out["preview_limits"] = tpl.get("preview_limits", [])
    out["simulated_steps"] = tpl.get("simulated_steps", [])
    out["provenance"] = prov
    out["blocked"] = blocked
    out["questions"] = questions
    out["status"] = "blocked" if blocked else ("needs_input" if questions else "resolved")
    return out


def resolution_digest(resolution: dict[str, Any]) -> str:
    return digest(resolution)


def summary(resolution: dict[str, Any]) -> dict[str, Any]:
    """Récapitulatif en langage clair pour l'écran « Vérifier et soumettre »."""
    if resolution.get("status") != "resolved":
        return {"status": resolution.get("status"), "blocked": resolution.get("blocked", []),
                "questions": resolution.get("questions", [])}
    n = resolution["quantity"]
    comps = ", ".join(c["title"] for c in resolution["components"])
    return {
        "status": "resolved",
        "headline": f"{n} machine{'s' if n > 1 else ''} « {resolution['template']['title']} » "
                    f"en {resolution['environment']['name']}",
        "creates": [vm["name"] for vm in resolution["vms"]],
        "service_level": f"{resolution['size']['label']} ({resolution['usage']['label']})",
        "software": comps,
        "validations": [c["type"] for c in resolution["verification"]],
        "approval": "approbation par un approbateur distinct requise"
        if resolution["approval"]["distinct_approver"] else "approbation requise",
        "lifetime_days": (resolution.get("lifetime") or {}).get("days"),
    }
