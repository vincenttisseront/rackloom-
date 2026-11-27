"""Résolveur central : du besoin (catégorie A) aux valeurs techniques (catégorie B).

Toutes les sources (WebUI, chat, API, CI, duplication, reprise) passent par `validate_need`
puis `resolve`. Le résolveur ne fait que des lectures (registre, politiques, octrois,
instantané d'inventaire) et produit le même résultat, empreinte comprise, pour les mêmes
entrées. Ordre (docs/lot-1-plan.md §7.4) :

1. contraintes obligatoires (bundle de plateforme, octrois, politique d'environnement,
   paramètres d'environnement du template) ; inventaire de plus de 24 h : blocage ;
2. environnement demandé, sinon l'unique environnement hors production admissible ;
   jamais de repli, jamais la production par défaut ;
3. version de template : dernière publiée (création) ou version épinglée (modification) ;
4. préférence de projet approuvée (F50), si applicable et dans les octrois ;
5. défaut du template ;
6. départage : rang, puis identifiant stable croissant ;
7. blocage expliqué si une dimension n'a aucun candidat.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from .. import inventory, ipam, policy, projects, registry
from ..canonical import digest
from ..config import get_settings
from ..db import Conn
from ..errors import ApiError
from ..identity.authz import Principal
from ..inventory import capacity as capacity_check
from ..projects import preferences as project_preferences

NEED_SCHEMA = "rackloom.need/v1"


RESOLUTION_SCHEMA = "rackloom.resolution/v1"


NEED_KEYS = {"schema", "project_id", "service", "environment", "confirm_production", "inputs"}


POINTERS = (
    "/service/template",
    "/environment",
    "/inputs/quantity",
    "/inputs/size_tier",
    "/inputs/usage",
    "/inputs/lifetime_until",
)


LOCKED_MESSAGE = "Ce réglage est déterminé par le profil de service."


TECH_TOKENS = {
    "vcpu",
    "vcpus",
    "cpu",
    "cpus",
    "core",
    "cores",
    "memory",
    "mem",
    "ram",
    "disk",
    "disks",
    "storage",
    "network",
    "networks",
    "net",
    "vlan",
    "portgroup",
    "ip",
    "ips",
    "ipv4",
    "ipv6",
    "dns",
    "hostname",
    "fqdn",
    "datastore",
    "cluster",
    "pool",
    "folder",
    "placement",
    "image",
    "runner",
    "backend",
    "role",
    "roles",
    "package",
    "packages",
    "software",
    "credential",
    "credentials",
    "password",
    "secret",
    "secrets",
    "token",
    "license",
    "licence",
    "licenses",
    "licences",
    "sku",
    "playbook",
    "provider",
    "providers",
    "terraform",
    "hcl",
    "ansible",
    "vsphere",
    "vcenter",
    "esxi",
    # Réglages des composants et de l'exécution par vagues (AT01) : publiés dans le template.
    "nginx",
    "fail2ban",
    "jail",
    "jails",
    "bantime",
    "maxretry",
    "findtime",
    "port",
    "ports",
    "firewall",
    "nftables",
    "iptables",
    "tls",
    "headers",
    "wave",
    "waves",
    "pilot",
}


TECH_KEYS = {
    "template_ref",
    "template_version",
    "version",
    "name",
    "names",
    "naming",
    "zone",
    "state",
    "state_backend",
    "module",
    "components",
    "component",
    "config",
    "backup",
    "quota",
    "approval",
    "approver",
    "policy",
    "health_checks",
    "verification",
    "outputs",
    "host",
    "hosts",
    "instances",
    "vm",
    "vms",
    "size",
    "tier_spec",
}


TECH_PREFIXES = ("ansible_", "tf_", "terraform_", "vsphere_")


BLOCK_MESSAGES = {
    "service_not_available": "Ce service n'est pas disponible pour ce projet.",
    "no_non_production_environment": "Aucun environnement hors production n'est disponible pour ce service.",
    "production_not_confirmed": "La production doit être demandée explicitement.",
    "no_published_version": "Aucune version publiée de ce service n'est utilisable.",
    "no_promoted_version": "Aucune version de ce service n'est promue pour cet environnement.",
    "no_authorized_image": "Aucune image compatible n'est disponible pour ce profil.",
    "no_authorized_network": "Aucun réseau n'est disponible pour ce profil.",
    "no_authorized_placement": "Aucun emplacement autorisé ne correspond à ce profil.",
    "anti_affinity_unsatisfiable": "Aucun placement autorisé ne sépare les réplicas comme l'exige le profil.",
    "inventory_stale": "Les données d'inventaire sont trop anciennes ; elles doivent être actualisées.",
    "name_unavailable": "Aucun nom conforme à la convention n'est disponible.",
    "capacity_below_threshold": "La capacité libre est sous le seuil sur le placement proposé.",
    "capacity_stale": "Les mesures de capacité sont trop anciennes.",
    "capacity_unknown": "Les mesures de capacité sont indisponibles sur le placement proposé.",
}


GROUPS = {
    "/service": "service",
    "/components": "service",
    "/placement/image": "placement",
    "/placement/cluster": "placement",
    "/placement/datastore": "placement",
    "/placement/folder": "placement",
    "/placement/instances": "placement",
    "/placement/network": "reseau",
    "/placement/addressing": "reseau",
    "/instances": "dimensionnement",
    "/execution": "execution",
    "/governance": "gouvernance",
}


def _group(pointer: str) -> str:
    for prefix, group in GROUPS.items():
        if pointer.startswith(prefix):
            return group
    return "service"


def _is_technical(key: str) -> bool:
    k = key.strip().lower().replace("-", "_")
    if k in TECH_KEYS or k.startswith(TECH_PREFIXES):
        return True
    return any(token in TECH_TOKENS for token in re.split(r"[^a-z0-9]+|_", k) if token)


def _scan_locked(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            pointer = f"{path}/{key}"
            if not isinstance(key, str):
                raise ApiError(422, "field_not_allowed", "clé non textuelle", field=path or "/")
            allowed_need_key = (
                (path == "" and key in NEED_KEYS)
                or (path == "/service" and key == "template")
                or (
                    path == "/inputs" and key in ("quantity", "size_tier", "usage", "lifetime_until", "source_instance")
                )
            )
            if not allowed_need_key and _is_technical(key):
                raise ApiError(422, "locked_field", LOCKED_MESSAGE, field=pointer)
            _scan_locked(item, pointer)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_locked(item, f"{path}/{index}")


def _bad(field: str, message: str, code: str = "invalid_input") -> ApiError:
    return ApiError(422, code, message, field=field)


def validate_need(raw: Any, partial: bool = False) -> dict[str, Any]:
    """V01 et V02 : forme exacte du besoin. Renvoie le besoin normalisé.

    `partial` admet un service non encore choisi (brouillon transmis à l'assistant)."""
    if not isinstance(raw, dict):
        raise _bad("/", "le besoin doit être un objet JSON", "field_not_allowed")
    _scan_locked(raw)
    for key in raw:
        if key not in NEED_KEYS:
            raise ApiError(422, "field_not_allowed", f"champ « {key} » hors du schéma du besoin", field=f"/{key}")
    if raw.get("schema") != NEED_SCHEMA:
        raise _bad("/schema", f"schéma attendu : {NEED_SCHEMA}")
    service = raw.get("service")
    if not isinstance(service, dict) or set(service) != {"template"}:
        extra = sorted(set(service) - {"template"}) if isinstance(service, dict) else []
        raise ApiError(
            422,
            "field_not_allowed",
            "service : seul « template » est attendu",
            field=f"/service/{extra[0]}" if extra else "/service",
        )
    if partial and service["template"] is None:
        pass
    elif not isinstance(service["template"], str) or not re.match(r"^[a-z][a-z0-9-]{1,40}$", service["template"]):
        raise _bad("/service/template", "identifiant de service invalide")
    env = raw.get("environment")
    if env is not None and (not isinstance(env, str) or not re.match(r"^[a-z][a-z0-9-]{1,40}$", env)):
        raise _bad("/environment", "environnement invalide")
    confirm = raw.get("confirm_production", False)
    if not isinstance(confirm, bool):
        raise _bad("/confirm_production", "booléen attendu")
    inputs = raw.get("inputs", {})
    if not isinstance(inputs, dict):
        raise _bad("/inputs", "objet attendu")
    project_id = raw.get("project_id")
    if project_id is not None:
        try:
            project_id = str(UUID(str(project_id)))
        except ValueError as exc:
            raise _bad("/project_id", "identifiant de projet invalide") from exc
    need = {
        "schema": NEED_SCHEMA,
        "service": {"template": service["template"]},
        "environment": env,
        "confirm_production": confirm,
        "inputs": dict(inputs),
    }
    if project_id:
        need["project_id"] = project_id
    return need


def validate_provenance(raw: Any, need: dict[str, Any]) -> dict[str, dict[str, str]]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _bad("/provenance", "objet attendu", "invalid_provenance")
    result: dict[str, dict[str, str]] = {}
    for pointer, entry in raw.items():
        if pointer not in POINTERS:
            raise ApiError(422, "field_not_allowed", f"pointeur de provenance inconnu {pointer}", field="/provenance")
        if not isinstance(entry, dict) or set(entry) != {"source"} or entry["source"] not in ("user", "suggestion"):
            raise _bad(f"/provenance{pointer}", "source attendue : user ou suggestion", "invalid_provenance")
        result[pointer] = {"source": entry["source"]}
    present = {"/service/template": True, "/environment": need["environment"] is not None}
    present.update({f"/inputs/{k}": True for k in need["inputs"]})
    for pointer, here in present.items():
        if here:
            result.setdefault(pointer, {"source": "user"})
    return {p: e for p, e in result.items() if present.get(p)}


def unconfirmed(provenance: dict[str, dict[str, str]], pointers: tuple[str, ...] = POINTERS) -> list[str]:
    return sorted(p for p, e in provenance.items() if e["source"] == "suggestion" and p in pointers)


def _block(code: str, referent: str, field: str | None = None) -> dict[str, Any]:
    entry = {"code": code, "message": f"{BLOCK_MESSAGES[code]} Contactez le référent du projet {referent}."}
    if field:
        entry["field"] = field
    return entry


def _rank(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda i: (int(i["attributes"].get("rank", 1000)), i["stable_id"]))


def _tags_match(item: dict[str, Any], wanted: dict[str, Any]) -> bool:
    tags = item["attributes"].get("tags", {})
    return isinstance(tags, dict) and all(tags.get(k) == v for k, v in wanted.items())


def _site_of(item: dict[str, Any]) -> str | None:
    tags = item["attributes"].get("tags", {})
    site = tags.get("site") if isinstance(tags, dict) else None
    return str(site) if site is not None else None


def _datastore_for(cluster: dict[str, Any], datastores: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Préfère un datastore du même site que le cluster ; sinon le premier classé."""
    site = _site_of(cluster)
    if site is not None:
        matched = [d for d in datastores if _site_of(d) == site]
        if matched:
            return matched[0]
        return None
    return datastores[0] if datastores else None


def _assign_placements(
    clusters: list[dict[str, Any]],
    datastores: list[dict[str, Any]],
    quantity: int,
    anti_affinity: dict[str, Any] | None,
) -> list[tuple[dict[str, Any], dict[str, Any]]] | str:
    """Place chaque réplica. Renvoie la liste (cluster, datastore) ou un code de blocage."""
    scope = (anti_affinity or {}).get("scope")
    if scope == "cluster" and quantity > len(clusters):
        return "anti_affinity_unsatisfiable"
    if not clusters or not datastores:
        return "no_authorized_placement"
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    used_clusters: set[str] = set()
    for index in range(quantity):
        if scope == "cluster":
            cluster = clusters[index]
            if cluster["stable_id"] in used_clusters:
                return "anti_affinity_unsatisfiable"
            used_clusters.add(cluster["stable_id"])
        else:
            cluster = clusters[0]
        datastore = _datastore_for(cluster, datastores)
        if datastore is None:
            return "no_authorized_placement"
        out.append((cluster, datastore))
    return out


def _used_names(conn: Conn, project_id: UUID, environment_id: UUID, change_id: UUID | None) -> set[str]:
    names = {r["name"] for r in conn.execute("SELECT name FROM hosts WHERE project_id = %s", (project_id,))}
    names |= {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM name_reservations WHERE project_id = %s AND environment_id = %s AND released_at IS NULL "
            "AND change_id IS DISTINCT FROM %s",
            (project_id, environment_id, change_id),
        )
    }
    return names


def _input(field: str, message: str) -> ApiError:
    return _bad(f"/inputs/{field}", message)


def _validate_inputs(
    contract: dict[str, Any],
    inputs: dict[str, Any],
    env_params: dict[str, Any] | None,
    rules: dict[str, Any] | None,
    deployment: dict[str, Any] | None,
    today: date,
) -> None:
    """V05, V08, V09, V10 sur les entrées fournies."""
    spec = contract["inputs"]
    # F40 : source_instance est un besoin fonctionnel de restauration, hors contrat template.
    restore_keys = {"source_instance"}
    for key in inputs:
        if key in restore_keys:
            if not isinstance(inputs[key], str) or not inputs[key].strip() or len(inputs[key]) > 80:
                raise _input("source_instance", "nom d'instance source attendu")
            continue
        if key not in spec:
            raise ApiError(
                422, "field_not_allowed", f"entrée « {key} » non déclarée par ce service", field=f"/inputs/{key}"
            )
        if deployment is not None and key in ("quantity", "size_tier") and not spec[key].get("modifiable"):
            raise ApiError(422, "field_not_allowed", f"entrée « {key} » non modifiable", field=f"/inputs/{key}")
    if "quantity" in inputs:
        q, s = inputs["quantity"], spec["quantity"]
        if not isinstance(q, int) or isinstance(q, bool) or not s["min"] <= q <= s["max"]:
            raise _input("quantity", f"nombre entier entre {s['min']} et {s['max']} attendu")
    if "size_tier" in inputs:
        t = inputs["size_tier"]
        if t not in spec["size_tier"]["values"]:
            raise _input("size_tier", "niveau inconnu pour ce service")
        if env_params is not None and t not in env_params["size_tiers"]:
            raise _input("size_tier", "ce niveau n'est pas proposé dans cet environnement")
    if "usage" in inputs:
        u = inputs["usage"]
        if not isinstance(u, str) or len(u) > spec["usage"].get("max_length", 80) or re.search(r"[\x00-\x1f\x7f<>]", u):
            raise _input("usage", "texte brut de 80 caractères au plus attendu")
    if "lifetime_until" in inputs:
        raw = inputs["lifetime_until"]
        try:
            until = date.fromisoformat(raw) if isinstance(raw, str) else None
        except ValueError:
            until = None
        if until is None:
            raise _input("lifetime_until", "date attendue au format AAAA-MM-JJ")
        if until <= today:
            raise _input("lifetime_until", "la date de fin doit être dans le futur")
        lifetime = (rules or {}).get("lifetime", {})
        if lifetime.get("max_days") and until > today + timedelta(days=lifetime["max_days"]):
            raise _input("lifetime_until", f"{lifetime['max_days']} jours au plus dans cet environnement")


def resolve(
    conn: Conn,
    *,
    principal: Principal,
    project: dict[str, Any],
    need: dict[str, Any],
    provenance: dict[str, dict[str, str]],
    change_id: UUID | None = None,
    deployment: dict[str, Any] | None = None,
    change_kind: str = "create",
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    today = now.date()
    settings = get_settings()
    referent_user = projects.referent_name(conn, project)
    referent = project["name"] + (f" ({referent_user})" if referent_user != "désigné du projet" else "")
    is_restore = change_kind == "restore"
    slug = need["service"]["template"]
    pref_row_early = project_preferences.current(conn, project["id"])
    pref_early = (pref_row_early["preferences"] if pref_row_early else {}) or {}
    template_from_preference = False
    if not slug and deployment is None:
        candidate = pref_early.get("default_service_template")
        if candidate and candidate in registry.granted_services(conn, project["id"]):
            slug = candidate
            template_from_preference = True
    if need.get("project_id") and need["project_id"] != str(project["id"]):
        raise _bad("/project_id", "le besoin désigne un autre projet")
    out: dict[str, Any] = {
        "schema": RESOLUTION_SCHEMA,
        "policy_bundle": policy.active(),
        "blocks": [],
        "missing": [],
        "values": {},
    }
    values = out["values"]

    def val(pointer: str, value: Any, source: str, **extra: Any) -> None:
        values[pointer] = {"value": value, "source": source, "group": _group(pointer), **extra}

    def finish(outcome: str) -> dict[str, Any]:
        out["outcome"] = outcome
        out["resolution_digest"] = resolution_digest(out)
        return out

    fresh = inventory.freshness(conn, settings.inventory_max_age_hours)
    out["inventory"] = {"snapshot_id": fresh["snapshot_id"], "observed_at": fresh["observed_at"], "simulated": True}

    # 1. Service octroyé ; version figée pour une modification, sinon choisie après l'environnement (F01).
    envs = projects.environments(conn, project["id"])
    version_source: str
    if deployment is not None:
        if slug != deployment["template_slug"]:
            raise _bad("/service/template", "une modification garde le service du déploiement", "field_not_allowed")
        tpl_row = registry.template_by_id(conn, deployment["template_version_id"])
        if tpl_row is None or tpl_row["status"] == "withdrawn":
            out["blocks"].append(_block("no_published_version", referent, "/service/template"))
            return finish("blocked")
        version_source = "deployment_pinned"
        contract = tpl_row["contract"]
        admissible = set(contract["deployment_policy"]["admissible_environment_kinds"]) & set(contract["environments"])
        env = next(e for e in envs if e["id"] == deployment["environment_id"])
        if need["environment"] not in (None, env["slug"]):
            raise _bad(
                "/environment", "une modification reste dans l'environnement du déploiement", "field_not_allowed"
            )
        env_source = "deployment_pinned"
    else:
        if not slug or slug not in registry.granted_services(conn, project["id"]):
            out["blocks"].append(_block("service_not_available", referent, "/service/template"))
            return finish("blocked")
        guide = registry.latest_published(conn, slug)
        if guide is None:
            out["blocks"].append(_block("no_published_version", referent, "/service/template"))
            return finish("blocked")
        guide_contract = guide["contract"]
        admissible = (
            set(guide_contract["deployment_policy"]["admissible_environment_kinds"])
            & set(guide_contract["environments"])
            & registry.promoted_kinds(conn, slug)
        )
        # 2. Environnement : demandé, sinon unique hors production ; jamais de repli.
        env = None
        if need["environment"] is not None:
            env = next((e for e in envs if e["slug"] == need["environment"]), None)
            if env is None:
                raise _bad("/environment", "environnement inconnu pour ce projet")
            if not principal.has_role("operator", project["id"], env["id"]):
                raise _bad(
                    "/environment",
                    "vous n'êtes pas autorisé à déployer dans cet environnement",
                    "environment_not_allowed",
                )
            if env["kind"] not in admissible:
                raise _bad(
                    "/environment",
                    "ce service n'est pas admissible dans cet environnement",
                    "environment_not_allowed",
                )
            if env["is_production"] and not need["confirm_production"]:
                raise _bad("/environment", BLOCK_MESSAGES["production_not_confirmed"], "production_not_confirmed")
            if slug not in registry.granted_services(conn, project["id"], env["id"]):
                out["blocks"].append(_block("service_not_available", referent, "/service/template"))
                return finish("blocked")
            env_source = provenance.get("/environment", {"source": "user"})["source"]
        else:
            if need["confirm_production"]:
                raise _bad("/environment", "indiquez l'environnement de production demandé")
            candidates = [
                e
                for e in envs
                if not e["is_production"]
                and e["kind"] in admissible
                and principal.has_role("operator", project["id"], e["id"])
                and slug in registry.granted_services(conn, project["id"], e["id"])
            ]
            if not candidates:
                out["blocks"].append(_block("no_non_production_environment", referent, "/environment"))
                return finish("blocked")
            if len(candidates) > 1:
                out["missing"].append(
                    {
                        "field": "/environment",
                        "text": "Dans quel environnement déployer ?",
                        "options": [e["slug"] for e in candidates],
                    }
                )
                return finish("incomplete")
            env = candidates[0]
            env_source = "environment_default"
        tpl_row = registry.latest_promoted(conn, slug, env["kind"])
        if tpl_row is None:
            out["blocks"].append(_block("no_promoted_version", referent, "/service/template"))
            return finish("blocked")
        version_source = "template_latest_promoted"
        contract = tpl_row["contract"]

    out["template"] = {
        "slug": slug,
        "version": contract["version"],
        "digest": tpl_row["digest"],
        "title": contract["title"],
        "description": contract["description"],
        "template_version_id": str(tpl_row["id"]),
    }
    template_source = (
        "project_preference"
        if template_from_preference
        else provenance.get("/service/template", {"source": "user"})["source"]
    )
    val("/service/template", slug, template_source, label=contract["title"])
    val("/service/version", contract["version"], version_source)

    pol = projects.current_policy(conn, env["id"])
    rules = pol["rules"] if pol else {}
    env_params = dict(contract["environments"][env["kind"]])
    # F40 : restauration → réseau de quarantaine uniquement, nom distinct, pas de prod.
    restore_meta: dict[str, Any] | None = None
    if is_restore:
        from ..restore import profile as restore_profile_mod

        profile = restore_profile_mod.restore_profile(contract)
        source_instance = str((need.get("inputs") or {}).get("source_instance") or "").strip()
        if not source_instance:
            raise _bad("/inputs/source_instance", "instance source obligatoire pour une restauration")
        placement = dict(env_params.get("placement") or {})
        placement["network_role"] = profile["network_role"]
        env_params["placement"] = placement
        env_params["naming"] = profile["naming"].replace("{env_slug}", "{environment}")
        env_params["backup"] = {
            "policy": profile["backup_policy"],
            "max_age_hours": profile["max_age_hours"],
        }
        restore_meta = {
            "source_instance": source_instance,
            "network_role": profile["network_role"],
            "attach_production_network": False,
            "simulated": True,
        }
    out["environment"] = {
        "id": str(env["id"]),
        "slug": env["slug"],
        "name": env["name"],
        "kind": env["kind"],
        "is_production": env["is_production"],
        "policy_version": pol["version"] if pol else None,
        "policy_digest": pol["digest"] if pol else None,
        "source": env_source,
    }
    val("/environment", env["slug"], env_source, label=f"{project['name']} / {env['name']}")
    val(
        "/governance/environment_policy",
        pol["version"] if pol else None,
        "environment_policy",
        ref=f"{env['slug']}@{pol['version']}" if pol else None,
    )
    val("/governance/policy_bundle", out["policy_bundle"]["version"], "platform")

    # 4. Préférence de projet approuvée (F50) : seulement si encore dans les octrois.
    pref_row = project_preferences.current(conn, project["id"])
    pref = (pref_row["preferences"] if pref_row else {}) or {}
    # Préférence de service hors octrois de cet environnement : ignorée (pas d'erreur).
    if pref.get("default_service_template") and pref["default_service_template"] not in registry.granted_services(
        conn, project["id"], env["id"]
    ):
        pref = {k: v for k, v in pref.items() if k != "default_service_template"}
    out["project_preference"] = (
        {"version": pref_row["version"], "digest": pref_row["digest"], "preferences": pref} if pref_row else None
    )

    # Entrées : validation, défauts du template et de la politique (après préférence).
    inputs = need["inputs"]
    _validate_inputs(contract, inputs, env_params, rules, deployment, today)
    spec = contract["inputs"]
    previous = {i["name"]: i for i in (deployment or {}).get("instances", [])}
    default_tier = env_params["default_size_tier"]
    tier_source_default = "template_default"
    pref_tier = pref.get("default_size_tier")
    if pref_tier and pref_tier in env_params["size_tiers"] and "size_tier" not in inputs and deployment is None:
        default_tier = pref_tier
        tier_source_default = "project_preference"
    if deployment is not None and previous and not is_restore:
        first = next(iter(previous.values()))
        default_tier = first.get("size_tier") or default_tier
        tier_source_default = "deployment_pinned"
    if is_restore:
        quantity = int(inputs.get("quantity") or 1)
    else:
        quantity = inputs.get("quantity", len(previous) if previous else spec["quantity"]["default"])
    tier = inputs.get("size_tier", default_tier)

    def src(field: str, default: str) -> str:
        return provenance.get(f"/inputs/{field}", {"source": "user"})["source"] if field in inputs else default

    tier_labels = spec["size_tier"].get("value_labels", {})
    out["inputs"] = {
        "quantity": quantity,
        "size_tier": tier,
        "size_label": tier_labels.get(tier, tier),
        "usage": inputs.get("usage", ""),
        "lifetime_until": inputs.get("lifetime_until"),
    }
    out["input_sources"] = {
        "quantity": src("quantity", "template_default"),
        "size_tier": src("size_tier", tier_source_default),
        "usage": src("usage", "template_default"),
        "lifetime_until": src("lifetime_until", "environment_policy_default"),
    }
    lifetime = rules.get("lifetime", {})
    if lifetime.get("required") and not inputs.get("lifetime_until"):
        out["missing"].append(
            {
                "field": "/inputs/lifetime_until",
                "text": "Jusqu'à quelle date en avez-vous besoin ?"
                + (f" ({lifetime['max_days']} jours au plus)" if lifetime.get("max_days") else ""),
            }
        )

    # 3. Placement depuis l'inventaire octroyé (dernier instantané), départage rang puis identifiant.
    # F02 : une composition fixe le nombre total d'instances (somme des membres), pas seulement quantity.
    from .. import composition as composition_mod

    compose_early = composition_mod.has_composition(contract)
    if compose_early:
        placement_quantity = sum(
            composition_mod.member_quantity(m, int(quantity)) for m in composition_mod.members(contract)
        )
    else:
        placement_quantity = int(quantity)
    if fresh["stale"]:
        out["blocks"].append(_block("inventory_stale", referent))
    granted = [] if fresh["stale"] else inventory.granted_items(conn, project["id"], env["id"])
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in granted:
        by_kind.setdefault(item["kind"], []).append(item)
    placement = env_params["placement"]
    flt = contract["creation_method"]["image_filter"]
    images = _rank([i for i in by_kind.get("image", []) if all(i["attributes"].get(k) == v for k, v in flt.items())])
    clusters = _rank([i for i in by_kind.get("cluster", []) if _tags_match(i, placement["cluster_tags"])])
    datastores = _rank([i for i in by_kind.get("datastore", []) if _tags_match(i, placement["datastore_tags"])])
    folder_name = placement["folder"].format(project=project["slug"], environment=env["slug"])
    folders = _rank([i for i in by_kind.get("folder", []) if i["name"] == folder_name])
    networks = _rank(
        [i for i in by_kind.get("network", []) if i["attributes"].get("role") == placement["network_role"]]
    )
    anti_affinity = contract["deployment_policy"].get("anti_affinity")
    if anti_affinity is not None and not isinstance(anti_affinity, dict):
        anti_affinity = None
    placements: list[tuple[dict[str, Any], dict[str, Any]]] | str | None = None
    if not fresh["stale"]:
        if not images:
            out["blocks"].append(_block("no_authorized_image", referent))
        if not networks:
            out["blocks"].append(_block("no_authorized_network", referent))
        if not folders:
            out["blocks"].append(_block("no_authorized_placement", referent))
        else:
            placements = _assign_placements(clusters, datastores, placement_quantity, anti_affinity)
            if isinstance(placements, str):
                out["blocks"].append(_block(placements, referent))
                placements = None
    if out["blocks"]:
        return finish("blocked")
    assert placements is not None

    def pick(pointer: str, items: list[dict[str, Any]], ref: str) -> dict[str, Any]:
        chosen = items[0]
        val(
            pointer,
            chosen["stable_id"],
            "tie_break" if len(items) > 1 else "inventory",
            label=chosen["name"],
            ref=ref,
            candidates=len(items),
        )
        return chosen

    image = pick("/placement/image", images, "creation_method.image_filter")
    folder = pick("/placement/folder", folders, f"environments.{env['kind']}.placement.folder")
    network = pick("/placement/network", networks, f"environments.{env['kind']}.placement.network_role")
    # Placement principal (1ʳᵉ instance) + détail par réplica pour F32 / R055.
    primary_cluster, primary_datastore = placements[0]
    cluster_src = "tie_break" if len(clusters) > 1 else "inventory"
    ds_src = "tie_break" if len(datastores) > 1 else "inventory"
    cluster_ref = f"environments.{env['kind']}.placement.cluster_tags"
    ds_ref = f"environments.{env['kind']}.placement.datastore_tags"
    val(
        "/placement/cluster",
        primary_cluster["stable_id"],
        cluster_src,
        label=primary_cluster["name"],
        ref=cluster_ref,
        candidates=len(clusters),
    )
    val(
        "/placement/datastore",
        primary_datastore["stable_id"],
        ds_src,
        label=primary_datastore["name"],
        ref=ds_ref,
        candidates=len(datastores),
    )
    for index, (cl, ds) in enumerate(placements):
        val(
            f"/placement/instances/{index}/cluster",
            cl["stable_id"],
            cluster_src,
            label=cl["name"],
            ref=cluster_ref,
            candidates=len(clusters),
        )
        val(
            f"/placement/instances/{index}/datastore",
            ds["stable_id"],
            ds_src,
            label=ds["name"],
            ref=ds_ref,
            candidates=len(datastores),
        )
    val(
        "/placement/addressing",
        contract["system_profile"]["network"]["addressing"],
        "template",
        ref="system_profile.network.addressing",
        label="attribuée par le laboratoire (prévisualisation IPAM, sans réservation)",
    )
    out["placement"] = {
        "image": {
            "stable_id": image["stable_id"],
            "name": image["name"],
            "guest_os": image["attributes"].get("guest_os"),
        },
        "cluster": {"stable_id": primary_cluster["stable_id"], "name": primary_cluster["name"]},
        "datastore": {"stable_id": primary_datastore["stable_id"], "name": primary_datastore["name"]},
        "folder": {"stable_id": folder["stable_id"], "name": folder["name"]},
        "network": {
            "stable_id": network["stable_id"],
            "name": network["name"],
            "role": (network.get("attributes") or {}).get("role"),
        },
        "addressing": contract["system_profile"]["network"]["addressing"],
    }
    if is_restore and restore_meta is not None:
        from ..restore import profile as restore_profile_mod

        restore_profile_mod.assert_isolated_network(out["placement"]["network"])
        previous = []
    # Hors de placement : le dossier d'exécution n'attend que des stable_id sous targets.placement.
    out["anti_affinity"] = {"scope": anti_affinity["scope"]} if anti_affinity and anti_affinity.get("scope") else None

    # Instances : noms existants conservés, nouveaux noms selon la convention (préférence F50 si valide).
    # F02 : une composition produit des instances par membre, ordonnées par dépendances.
    dims = env_params["size_tiers"][tier]
    naming_pattern = env_params["naming"]
    naming_source = "template"
    naming_ref = f"environments.{env['kind']}.naming"
    if pref.get("naming") and deployment is None:
        naming_pattern = pref["naming"]
        naming_source = "project_preference"
        naming_ref = f"project_preference@{pref_row['version']}" if pref_row else "project_preference"
    used = _used_names(conn, project["id"], env["id"], change_id) - set(previous)
    compose = compose_early
    member_plan: list[tuple[dict[str, Any], int]] = []
    if compose:
        for mid in composition_mod.topological_member_order(contract):
            mdef = composition_mod.member_by_id(contract, mid)
            assert mdef is not None
            member_plan.append((mdef, composition_mod.member_quantity(mdef, int(quantity))))

    names: list[str] = []
    instance_members: list[str | None] = []
    if compose:
        previous_by_member: dict[str, list[str]] = {}
        for pname, pinfo in previous.items():
            pm = (pinfo or {}).get("member") if isinstance(pinfo, dict) else None
            if pm:
                previous_by_member.setdefault(pm, []).append(pname)
        nn_by_member: dict[str, int] = {m["id"]: 1 for m, _ in member_plan}
        for mdef, mqty in member_plan:
            mid = mdef["id"]
            kept = list(previous_by_member.get(mid, []))[:mqty]
            names.extend(kept)
            instance_members.extend([mid] * len(kept))
            while len([x for x in instance_members if x == mid]) < mqty:
                if nn_by_member[mid] > 99:
                    out["blocks"].append(_block("name_unavailable", referent))
                    return finish("blocked")
                fmt_kwargs = {
                    "project_code": project["code"],
                    "nn": f"{nn_by_member[mid]:02d}",
                    "environment": env["slug"],
                    "project": project["slug"],
                    "member": mid,
                }
                nn_by_member[mid] += 1
                try:
                    candidate = naming_pattern.format(**fmt_kwargs)
                except KeyError:
                    candidate = naming_pattern.replace("{member}", mid).format(
                        project_code=project["code"],
                        nn=f"{nn_by_member[mid] - 1:02d}",
                        environment=env["slug"],
                        project=project["slug"],
                    )
                if candidate not in used and candidate not in names:
                    names.append(candidate)
                    instance_members.append(mid)
    else:
        names = list(previous)[:quantity]
        nn = 1
        while len(names) < quantity:
            if nn > 99:
                out["blocks"].append(_block("name_unavailable", referent))
                return finish("blocked")
            candidate = naming_pattern.format(
                project_code=project["code"],
                nn=f"{nn:02d}",
                environment=env["slug"],
                project=project["slug"],
            )
            nn += 1
            if candidate not in used and candidate not in names:
                names.append(candidate)
        instance_members = [None] * len(names)

    tier_ref = f"environments.{env['kind']}.size_tiers.{tier}"
    out["instances"] = []
    for index, name in enumerate(names):
        cl, ds = placements[index]
        inst: dict[str, Any] = {
            "name": name,
            "size_tier": tier,
            **dims,
            "existing": name in previous,
            "cluster": {"stable_id": cl["stable_id"], "name": cl["name"]},
            "datastore": {"stable_id": ds["stable_id"], "name": ds["name"]},
        }
        if instance_members[index]:
            inst["member"] = instance_members[index]
        out["instances"].append(inst)
        val(f"/instances/{index}/name", name, naming_source, ref=naming_ref)
        if instance_members[index]:
            val(f"/instances/{index}/member", instance_members[index], "template", ref="composition.members")
        for key in ("vcpu", "memory_mb", "disk_gb"):
            val(f"/instances/{index}/{key}", dims[key], "template", ref=tier_ref)
    waves_spec = contract["deployment_policy"]["waves"]
    if compose:
        out["waves"] = composition_mod.plan_composition_waves(contract, out["instances"])
        wave_label = "composition : un membre après l'autre selon les dépendances et la readiness"
        wave_ref = "composition.members"
    else:
        out["waves"] = registry.plan_waves(waves_spec, names)
        wave_label = (
            "un pilote, puis les vagues suivantes ; un échec arrête la suite"
            if waves_spec["mode"] == "pilot"
            else "toutes les machines ensemble"
        )
        wave_ref = "deployment_policy.waves"
    val(
        "/execution/waves",
        " ; ".join(", ".join(w) for w in out["waves"]),
        "template",
        ref=wave_ref,
        label=wave_label,
    )
    if previous:
        # Annoncé dès l'étape 2 ; le plan correspondant sera bloqué (aucune suppression au lot 1).
        out["modification"] = {
            "removes": [n for n in previous if n not in names],
            "replaces": [n for n in names if n in previous and previous[n]["size_tier"] != tier],
        }

    # Composants, module, rôles, exécution, gouvernance.
    comps = registry.template_components(conn, contract)
    out["components"] = comps
    if compose:
        member_components: dict[str, list[dict[str, Any]]] = {}
        for mdef, _mqty in member_plan:
            # Résout les composants du membre (même forme que template_components).
            member_contract = {**contract, "components": mdef["components"]}
            member_components[mdef["id"]] = registry.template_components(conn, member_contract)
        out["member_components"] = member_components
        out["composition"] = {
            "members": [
                {
                    "id": m["id"],
                    "title": m["title"],
                    "depends_on": list(m.get("depends_on") or []),
                    "readiness": list(m.get("readiness") or []),
                    "health_checks": list(m.get("health_checks") or []),
                }
                for m in composition_mod.members(contract)
            ]
        }
    for c in comps:
        val(
            f"/components/{c['slug']}",
            c["version"],
            "template",
            label=c["title"],
            ref=f"rôle {c['role']['name']}@{c['role']['version']}",
        )
    tf = contract["terraform"]
    module = registry.artifact(conn, "tf_module", tf["module"], tf["version"])
    playbooks = {k: registry.artifact(conn, "playbook", k, v) for k, v in registry.PLAYBOOKS.items()}
    out["terraform"] = {
        "module": tf["module"],
        "version": tf["version"],
        "digest": module["digest"],
        "lockfile_sha256": module["lockfile_sha256"],
    }
    out["ansible"] = {
        "roles": [c["role"] for c in comps],
        "playbooks": {k: {"version": v["version"], "digest": v["digest"]} for k, v in playbooks.items()},
    }
    val("/execution/terraform_module", f"{tf['module']}@{tf['version']}", "template", ref="terraform")
    val("/execution/runner_terraform", "terraform (réseau : aucun)", "platform")
    val("/execution/runner_ansible", "ansible (réseau : rn-lab)", "platform")
    val("/execution/state_backend", "local_demo", "platform", label="state de démo, un dossier par déploiement")
    val("/execution/identity_configuration", "rackloom-config", "platform", label="élévation requise")
    val("/execution/identity_collect", "rackloom-collect", "platform", label="sans élévation")
    approval = rules.get("approval", {})
    distinct = bool(
        env["requires_distinct_approver"]
        or approval.get("distinct_approver")
        or contract["deployment_policy"]["approval"].get("distinct_approver")
    )
    out["approval"] = {
        "required": True,
        "distinct_approver": distinct,
        "approver_role": "approver",
        "production": env["is_production"],
    }
    val(
        "/governance/approval",
        "approbateur distinct" if distinct else "approbateur",
        "environment_policy",
        label=f"un approbateur de {project['name']} autre que le demandeur" if distinct else None,
    )
    backup_raw = env_params.get("backup", "none")
    if backup_raw in (None, "none", False):
        out["backup"] = {"policy": "none", "required": False}
    elif isinstance(backup_raw, dict):
        out["backup"] = {
            "policy": backup_raw.get("policy") or "daily",
            "max_age_hours": int(backup_raw.get("max_age_hours") or 24),
            "required": True,
        }
    else:
        out["backup"] = {"policy": str(backup_raw), "max_age_hours": 24, "required": True}
    val("/governance/backup", backup_raw, "template", applied=False)
    if restore_meta is not None:
        out["restore"] = restore_meta
        out["inputs"]["source_instance"] = restore_meta["source_instance"]
        for inst in out.get("instances") or []:
            if inst.get("name") == restore_meta["source_instance"]:
                out["blocks"].append(_block("name_unavailable", referent))
                return finish("blocked")
    val("/governance/open_ports", contract["system_profile"]["security"]["open_ports"], "template", applied=False)
    if out["inputs"]["lifetime_until"]:
        val(
            "/governance/lifetime_until",
            out["inputs"]["lifetime_until"],
            out["input_sources"]["lifetime_until"],
            applied=False,
        )
    out["health_checks"] = contract["health_checks"]
    out["preview_limits"] = contract.get("preview_limits", [])
    out["simulated_steps"] = contract.get("simulated_steps", [])
    out["outputs_allowlist"] = contract["outputs_allowlist"]
    # F03 : prévisualisation IPAM pour les noms résolus — aucune écriture de réservation.
    new_names = [inst["name"] for inst in out["instances"] if not inst.get("existing")]
    ipam_previews = ipam.preview_many(conn, hostnames=new_names) if new_names else []
    out["placement"]["ipam_preview"] = {
        "reservation": False,
        "simulated": True,
        "notice": "Prévisualisation seule : aucune réservation créée.",
        "candidates": [
            {"address": p["address"], "dns_name": p["dns_name"], "hostname": p["hostname"]} for p in ipam_previews
        ],
    }
    # F05 : mesure de capacité (âge, marge) — jamais présentée comme une réservation.
    cap = capacity_check.evaluate(
        conn,
        cluster_id=primary_cluster["stable_id"],
        datastore_id=primary_datastore["stable_id"],
        demand=capacity_check.demand_from_instances(out["instances"]),
        now=now,
    )
    out["capacity"] = cap
    if not cap["ok"]:
        code = (cap.get("refusal") or {}).get("code", "capacity_below_threshold")
        out["blocks"].append(_block(code, referent, "/placement/cluster"))
        return finish("blocked")
    # F34 : exigences figées dans la révision (pas de recalcul silencieux à l'admission).
    license_specs = contract["deployment_policy"].get("licenses") or []
    out["licenses"] = [
        {"sku": str(s["sku"]), "per_instance": int(s.get("per_instance", 1))}
        for s in license_specs
        if isinstance(s, dict) and s.get("sku")
    ]
    return finish("incomplete" if out["missing"] else "resolved")


def resolution_digest(resolution: dict[str, Any]) -> str:
    return "sha256:" + digest({k: v for k, v in resolution.items() if k != "resolution_digest"})


CHECK_LABELS = {
    "package_installed": "paquet {package} installé",
    "service_active": "service {unit} actif",
    "http_probe": "page HTTP {expect_status}",
    "jail_active": "jail Fail2ban {jail} active",
}


def summary(resolution: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    """Récapitulatif en langage clair pour l'étape « Vérifier et soumettre »."""
    if resolution["outcome"] == "blocked":
        return {"outcome": "blocked", "blocks": resolution["blocks"]}
    if resolution["outcome"] == "incomplete" and "instances" not in resolution:
        return {"outcome": "incomplete", "missing": resolution["missing"]}
    n = len(resolution["instances"])
    title = resolution["template"]["title"]
    noun = title[0].lower() + title[1:]
    if n > 1:
        first, _, rest = noun.partition(" ")
        noun = f"{first}s {rest}".strip()
    comps = ", ".join(c["slug"].capitalize() for c in resolution["components"])
    os_parts = (resolution["placement"]["image"].get("guest_os") or "").replace(" LTS", "").split()
    if os_parts and os_parts[0].lower() in title.lower().split():
        os_parts = os_parts[1:]
    os_label = " ".join(os_parts)
    env = resolution["environment"]
    until = resolution["inputs"].get("lifetime_until")
    until_txt = f", jusqu'au {date.fromisoformat(until).strftime('%d/%m/%Y')}" if until else ""
    headline = (
        f"{n} {noun} {os_label} avec {comps} dans {project['name']} / {env['name']}, "
        f"niveau {resolution['inputs']['size_label']}{until_txt}."
    ).replace("  ", " ")
    checks = ", ".join(CHECK_LABELS[c["type"]].format(**c) for c in resolution["health_checks"])
    approver = (
        f"un approbateur de {project['name']} autre que vous"
        if resolution["approval"]["distinct_approver"]
        else f"un approbateur de {project['name']}"
    )
    if env["is_production"]:
        approver += " (circuit de production)"
    waves = resolution.get("waves") or []
    waves_text = None
    if len(waves) > 1:
        waves_text = (
            " ; ".join(f"{'pilote' if i == 0 else f'vague {i + 1}'} : {', '.join(w)}" for i, w in enumerate(waves))
            + ". Un échec arrête les vagues suivantes."
        )
    return {
        "outcome": resolution["outcome"],
        "headline": headline,
        "verifications": checks,
        "approval": approver,
        "waves": waves_text,
        "creates": [i["name"] for i in resolution["instances"] if not i["existing"]],
        "keeps": [i["name"] for i in resolution["instances"] if i["existing"]],
        "removes": resolution.get("modification", {}).get("removes", []),
        "replaces": resolution.get("modification", {}).get("replaces", []),
        "production": env["is_production"],
        "missing": resolution["missing"],
        "simulated_steps": resolution["simulated_steps"],
        "preview_limits": resolution["preview_limits"],
    }
