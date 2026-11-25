"""Registre : composants versionnés, templates Rackloom versionnés et artefacts épinglés.

- Un **composant** déclare l'OS pris en charge, les prérequis, un contrat de paramètres,
  ses dépendances et incompatibilités, ses effets, ses contrôles et son implémentation
  (rôle Ansible épinglé).
- Un **template** combine une méthode de création, un profil système, des composants
  configurés, un contrat d'entrées fonctionnelles, des paramètres par type d'environnement,
  des contrôles de santé et une politique de déploiement. Ses versions **publiées** sont
  les profils de service lus par le résolveur.
- Une image vSphere n'est qu'un point de départ inventorié, jamais un template Rackloom.

`load` valide le dépôt `catalog/` et enregistre les versions au statut `validated` sans rien
publier. La publication est une action d'administration distincte, auditée, avec motif ;
elle ne crée aucun job et ne modifie ni plan ni déploiement.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import yaml

from .. import audit
from ..canonical import digest, file_tree_digest
from ..db import Jsonb
from ..errors import ApiError
from ..identity.authz import Principal, require_publisher
from ..ids import uuid7

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
INPUT_NAMES = {"quantity", "size_tier", "usage", "lifetime_until"}
PARAM_TYPES = {"integer", "boolean", "enum", "string", "secret_ref"}
SECRET_KEY_RE = re.compile(r"(pass(word)?|secret|token|private_key|api_key|credential)", re.I)
# Playbooks génériques de la plateforme (les rôles viennent des composants du template).
PLAYBOOKS = {"configure": "1.0.0", "collect": "1.0.0"}


class CatalogError(Exception):
    pass


def semver_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise CatalogError(f"{path} : document YAML attendu")
    return _plain(data)


def _plain(value: Any) -> Any:
    """YAML peut produire des dates : on les ramène en texte pour le JSON canonique."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


def _check_lockfile(module_dir: Path) -> dict[str, Any]:
    lock = module_dir / ".terraform.lock.hcl"
    if not lock.exists():
        raise CatalogError(f"{module_dir} : .terraform.lock.hcl absent")
    text = lock.read_text(encoding="utf-8")
    providers = [{"source": m.group(1), "version": m.group(2)}
                 for m in re.finditer(r'provider "([^"]+)" \{\s*version\s*=\s*"([^"]+)"', text)]
    versions_tf = (module_dir / "versions.tf").read_text(encoding="utf-8")
    for match in re.finditer(r'(?<![\w])version\s*=\s*"([^"]+)"', versions_tf):
        if not VERSION_RE.match(match.group(1)):
            raise CatalogError(f"{module_dir} : version de provider non fixe ({match.group(1)})")
    if not providers:
        raise CatalogError(f"{module_dir} : aucun provider verrouillé")
    for tf in module_dir.glob("*.tf"):
        body = tf.read_text(encoding="utf-8")
        if re.search(r'\bprovisioner\s+"', body) or re.search(r'\bsource\s*=\s*"(git::|https?://)', body):
            raise CatalogError(f"{tf} : provisioner ou module distant interdit")
    return {"providers": providers}


# --- Contrats ---------------------------------------------------------------------------

def _require(m: dict[str, Any], keys: tuple[str, ...], what: str) -> None:
    for key in keys:
        if key not in m:
            raise CatalogError(f"{what} : champ « {key} » manquant")


def _reject_secret_literals(value: Any, where: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)) and not (isinstance(item, str) and item.startswith("secret://")) \
                    and not isinstance(item, (dict, list, bool)):
                raise CatalogError(f"{where}.{key} : valeur littérale de type secret ; une référence secret:// "
                                   "est exigée")
            _reject_secret_literals(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_literals(item, f"{where}[{index}]")


def validate_component(m: dict[str, Any]) -> None:
    _require(m, ("schema", "slug", "version", "title", "supports", "parameters", "effects", "checks",
                 "implementation"), "composant")
    if m["schema"] != "rackloom.component/v1" or not VERSION_RE.match(str(m["version"])) \
            or not SLUG_RE.match(str(m["slug"])):
        raise CatalogError("composant : schéma, slug ou version invalide")
    for name, spec in m["parameters"].items():
        if spec.get("type") not in PARAM_TYPES:
            raise CatalogError(f"composant {m['slug']} : type du paramètre {name} non pris en charge")
        if "default" in spec and check_parameter(spec, spec["default"]):
            raise CatalogError(f"composant {m['slug']} : défaut invalide pour {name}")
    _reject_secret_literals(m.get("parameters", {}), f"composant {m['slug']}")


def check_parameter(spec: dict[str, Any], value: Any) -> str | None:
    t = spec["type"]
    if t == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return "entier attendu"
        if "min" in spec and value < spec["min"] or "max" in spec and value > spec["max"]:
            return f"hors bornes [{spec.get('min')}, {spec.get('max')}]"
    elif t == "boolean" and not isinstance(value, bool):
        return "booléen attendu"
    elif t == "enum" and value not in spec.get("values", []):
        return f"valeur hors de {spec.get('values')}"
    elif t == "string" and not isinstance(value, str):
        return "texte attendu"
    elif t == "secret_ref" and not (isinstance(value, str) and value.startswith("secret://")):
        return "référence secret:// attendue, jamais une valeur"
    return None


def check_component_config(component: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Configuration déclarée d'un composant contre son contrat. Paramètre inconnu : non pris en charge."""
    problems = []
    params = component["parameters"]
    for key, value in config.items():
        spec = params.get(key)
        if spec is None:
            problems.append(f"paramètre « {key} » non pris en charge par {component['slug']}@"
                            f"{component['version']}")
            continue
        error = check_parameter(spec, value)
        if error:
            problems.append(f"{component['slug']}.{key} : {error}")
    return problems


def validate_template(m: dict[str, Any]) -> None:
    _require(m, ("schema", "slug", "version", "title", "description", "creation_method", "system_profile",
                 "components", "inputs", "environments", "health_checks", "deployment_policy", "terraform",
                 "outputs_allowlist"), "template")
    if m["schema"] != "rackloom.template/v1" or not SLUG_RE.match(str(m["slug"])):
        raise CatalogError("template : schéma ou slug invalide")
    if not VERSION_RE.match(str(m["version"])):
        raise CatalogError("template : version non conforme (x.y.z)")
    if m["creation_method"].get("kind") != "clone_image":
        raise CatalogError(f"template : méthode de création « {m['creation_method'].get('kind')} » non prise en "
                           "charge au lot 1")
    unknown = set(m["inputs"]) - INPUT_NAMES
    if unknown:
        raise CatalogError(f"template : entrée(s) non prise(s) en charge : {sorted(unknown)}")
    q = m["inputs"].get("quantity", {})
    if q and not (1 <= q["min"] <= q["default"] <= q["max"] <= 10):
        raise CatalogError("template : bornes de quantité invalides")
    tiers = set(m["inputs"].get("size_tier", {}).get("values", []))
    kinds = set(m["deployment_policy"].get("admissible_environment_kinds", []))
    if m["deployment_policy"].get("waves", {}).get("mode") != "single":
        raise CatalogError("template : seules les vagues « single » sont prises en charge au lot 1")
    for kind, env in m["environments"].items():
        if kind not in kinds:
            raise CatalogError(f"template : paramètres pour un type d'environnement non admissible ({kind})")
        _require(env, ("size_tiers", "default_size_tier", "placement", "tie_break", "naming"), f"template.{kind}")
        if not set(env["size_tiers"]) <= tiers or env["default_size_tier"] not in env["size_tiers"]:
            raise CatalogError(f"template.{kind} : niveaux incohérents avec le contrat d'entrées")
        if "{nn}" not in env["naming"]:
            raise CatalogError(f"template.{kind} : convention de nommage sans {{nn}}")
        if env["tie_break"] != ["rank", "stable_id"]:
            raise CatalogError(f"template.{kind} : départage non pris en charge {env['tie_break']}")
    if not set(m["outputs_allowlist"]) <= {"name", "vm_uuid"}:
        raise CatalogError("template : sortie publiable non prise en charge")
    for ref in m.get("secret_refs", []):
        if not str(ref).startswith("secret://"):
            raise CatalogError("template : les secrets sont des références secret://, jamais des valeurs")
    _reject_secret_literals(m["components"], f"template {m['slug']}")


# --- Lecture du dépôt -------------------------------------------------------------------

def scan(catalog_dir: Path) -> dict[str, list[dict[str, Any]]]:
    artifacts: list[dict[str, Any]] = []
    for path in sorted((catalog_dir / "terraform").glob("*/*")):
        if path.is_dir():
            artifacts.append({
                "kind": "tf_module", "slug": path.parent.name, "version": path.name,
                "digest": file_tree_digest(path, ("*.tf", ".terraform.lock.hcl")),
                "lockfile_sha256": file_tree_digest(path, (".terraform.lock.hcl",)),
                "manifest": _check_lockfile(path),
                "source_ref": f"catalog/terraform/{path.parent.name}/{path.name}",
            })
    for path in sorted((catalog_dir / "ansible" / "roles").glob("*/*")):
        meta = path / "meta" / "rackloom.yml"
        if not meta.exists():
            raise CatalogError(f"{path} : contrat de rôle meta/rackloom.yml absent")
        artifacts.append({
            "kind": "ansible_role", "slug": path.parent.name, "version": path.name, "lockfile_sha256": None,
            "digest": file_tree_digest(path), "manifest": _read_yaml(meta),
            "source_ref": f"catalog/ansible/roles/{path.parent.name}/{path.name}",
        })
    for path in sorted((catalog_dir / "ansible" / "playbooks").glob("*/*")):
        artifacts.append({
            "kind": "playbook", "slug": path.parent.name, "version": path.name, "lockfile_sha256": None,
            "digest": file_tree_digest(path), "manifest": {"file": f"{path.parent.name}.yml"},
            "source_ref": f"catalog/ansible/playbooks/{path.parent.name}/{path.name}",
        })
    components = []
    for path in sorted((catalog_dir / "components").glob("*/*/component.yaml")):
        m = _read_yaml(path)
        validate_component(m)
        if m["slug"] != path.parent.parent.name or m["version"] != path.parent.name:
            raise CatalogError(f"{path} : slug ou version incohérents avec le chemin")
        components.append({"slug": m["slug"], "version": m["version"], "contract": m, "digest": digest(m),
                           "implementation": m["implementation"],
                           "source_ref": f"catalog/components/{m['slug']}/{m['version']}"})
    templates = []
    for path in sorted((catalog_dir / "templates").glob("*/*/template.yaml")):
        m = _read_yaml(path)
        validate_template(m)
        if m["slug"] != path.parent.parent.name or m["version"] != path.parent.name:
            raise CatalogError(f"{path} : slug ou version incohérents avec le chemin")
        templates.append({"slug": m["slug"], "version": m["version"], "contract": m, "digest": digest(m),
                          "source_ref": f"catalog/templates/{m['slug']}/{m['version']}"})
    art_index = {(a["kind"], a["slug"], a["version"]): a for a in artifacts}
    comp_index = {(c["slug"], c["version"]): c["contract"] for c in components}
    for c in components:
        impl = c["implementation"]
        role = art_index.get(("ansible_role", impl["ansible_role"], impl["version"]))
        if role is None:
            raise CatalogError(f"composant {c['slug']}@{c['version']} : rôle {impl} absent")
        if set(role["manifest"].get("parameters", [])) != set(c["contract"]["parameters"]):
            raise CatalogError(f"composant {c['slug']}@{c['version']} : paramètres du rôle différents du contrat")
    for t in templates:
        check_template_references(t["contract"], lambda s, v: comp_index.get((s, v)),
                                  lambda k, s, v: art_index.get((k, s, v)))
    for name, version in PLAYBOOKS.items():
        if ("playbook", name, version) not in art_index:
            raise CatalogError(f"playbook de plateforme {name}@{version} absent")
    return {"artifacts": artifacts, "components": components, "templates": templates}


def check_template_references(m: dict[str, Any], component_lookup, artifact_lookup) -> None:
    tf = m["terraform"]
    if artifact_lookup("tf_module", tf["module"], tf["version"]) is None:
        raise CatalogError(f"template {m['slug']}@{m['version']} : module {tf['module']}@{tf['version']} absent")
    os_ = m["system_profile"]["os"]
    slugs = set()
    for ref in m["components"]:
        comp = component_lookup(ref["component"], ref["version"])
        if comp is None:
            raise CatalogError(f"template {m['slug']}@{m['version']} : composant {ref['component']}@"
                               f"{ref['version']} absent du registre")
        problems = check_component_config(comp, ref.get("config", {}))
        if problems:
            raise CatalogError(f"template {m['slug']}@{m['version']} : " + "; ".join(problems))
        if os_ not in comp["supports"]["os"]:
            raise CatalogError(f"template {m['slug']}@{m['version']} : composant {comp['slug']} incompatible "
                               f"avec {os_}")
        slugs.add(comp["slug"])
    for ref in m["components"]:
        comp = component_lookup(ref["component"], ref["version"])
        for other in comp.get("incompatible_with", []):
            if other in slugs:
                raise CatalogError(f"template {m['slug']} : {comp['slug']} incompatible avec {other}")
        for dep in comp.get("depends_on", []):
            if dep not in slugs:
                raise CatalogError(f"template {m['slug']} : {comp['slug']} dépend de {dep}")


def load(conn: psycopg.Connection, catalog_dir: Path) -> dict[str, int]:
    """Valide et enregistre `catalog/` au statut `validated`. Ne publie rien."""
    found = scan(catalog_dir)
    counts = {"artifacts": 0, "components": 0, "templates": 0}
    for a in found["artifacts"]:
        row = artifact(conn, a["kind"], a["slug"], a["version"])
        if row:
            if row["digest"] != a["digest"]:
                raise CatalogError(f"{a['kind']} {a['slug']}@{a['version']} modifié après chargement : "
                                   "publiez une nouvelle version")
            continue
        conn.execute(
            "INSERT INTO catalog_artifacts (id, kind, slug, version, digest, lockfile_sha256, manifest, source_ref) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (uuid7(), a["kind"], a["slug"], a["version"], a["digest"], a["lockfile_sha256"], Jsonb(a["manifest"]),
             a["source_ref"]))
        counts["artifacts"] += 1
    for c in found["components"]:
        row = component(conn, c["slug"], c["version"])
        if row:
            if row["digest"] != c["digest"]:
                raise CatalogError(f"composant {c['slug']}@{c['version']} modifié après chargement : "
                                   "publiez une nouvelle version")
            continue
        conn.execute(
            "INSERT INTO component_versions (id, slug, version, contract, implementation, digest, source_ref) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (uuid7(), c["slug"], c["version"], Jsonb(c["contract"]), Jsonb(c["implementation"]), c["digest"],
             c["source_ref"]))
        counts["components"] += 1
    for t in found["templates"]:
        row = template(conn, t["slug"], t["version"])
        if row:
            if row["digest"] != t["digest"]:
                raise CatalogError(f"template {t['slug']}@{t['version']} modifié après chargement : "
                                   "publiez une nouvelle version")
            continue
        tid = uuid7()
        conn.execute(
            "INSERT INTO template_versions (id, slug, version, contract, digest, source_ref) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (tid, t["slug"], t["version"], Jsonb(t["contract"]), t["digest"], t["source_ref"]))
        conn.execute("INSERT INTO template_events (template_version_id, action, reason) "
                     "VALUES (%s, 'loaded', 'chargement de catalog/')", (tid,))
        counts["templates"] += 1
    return counts


# --- Requêtes ---------------------------------------------------------------------------

def artifact(conn: psycopg.Connection, kind: str, slug: str, version: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM catalog_artifacts WHERE kind = %s AND slug = %s AND version = %s",
                        (kind, slug, version)).fetchone()


def component(conn: psycopg.Connection, slug: str, version: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM component_versions WHERE slug = %s AND version = %s",
                        (slug, version)).fetchone()


def template(conn: psycopg.Connection, slug: str, version: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM template_versions WHERE slug = %s AND version = %s",
                        (slug, version)).fetchone()


def template_by_id(conn: psycopg.Connection, template_version_id: UUID) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM template_versions WHERE id = %s", (template_version_id,)).fetchone()


def latest_published(conn: psycopg.Connection, slug: str) -> dict[str, Any] | None:
    rows = conn.execute("SELECT * FROM template_versions WHERE slug = %s AND status = 'published'",
                        (slug,)).fetchall()
    return max(rows, key=lambda r: semver_key(r["version"])) if rows else None


def granted_services(conn: psycopg.Connection, project_id: UUID,
                     environment_id: UUID | None = None) -> list[str]:
    query = ("SELECT DISTINCT stable_id FROM resource_grants WHERE kind = 'service_template' AND project_id = %s "
             "AND revoked_at IS NULL")
    params: list[Any] = [project_id]
    if environment_id is not None:
        query += " AND environment_id = %s"
        params.append(environment_id)
    return [r["stable_id"] for r in conn.execute(query + " ORDER BY stable_id", params).fetchall()]


def template_components(conn: psycopg.Connection, contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Composants épinglés par une version de template, configuration complète (défauts inclus)."""
    result = []
    for ref in contract["components"]:
        row = component(conn, ref["component"], ref["version"])
        if row is None:
            raise CatalogError(f"composant {ref['component']}@{ref['version']} absent du registre")
        c = row["contract"]
        config = {k: v["default"] for k, v in c["parameters"].items() if "default" in v}
        config.update(ref.get("config", {}))
        impl = c["implementation"]
        role = artifact(conn, "ansible_role", impl["ansible_role"], impl["version"])
        result.append({"slug": c["slug"], "version": c["version"], "title": c["title"], "digest": row["digest"],
                       "status": row["status"], "verification_level": row["verification_level"],
                       "config": config, "effects": c["effects"], "checks": c["checks"],
                       "role": {"name": impl["ansible_role"], "version": impl["version"],
                                "digest": role["digest"] if role else None}})
    return result


# --- Publication ------------------------------------------------------------------------

def impact(conn: psycopg.Connection, slug: str, version: str) -> dict[str, Any]:
    projects = [r["slug"] for r in conn.execute(
        "SELECT DISTINCT p.slug FROM resource_grants g JOIN projects p ON p.id = g.project_id "
        "WHERE g.kind = 'service_template' AND g.stable_id = %s AND g.revoked_at IS NULL ORDER BY p.slug",
        (slug,)).fetchall()]
    deployments = conn.execute(
        "SELECT d.id, p.slug AS project, e.slug AS environment, t.version FROM deployments d "
        "JOIN template_versions t ON t.id = d.template_version_id JOIN projects p ON p.id = d.project_id "
        "JOIN environments e ON e.id = d.environment_id WHERE d.template_slug = %s ORDER BY d.created_at",
        (slug,)).fetchall()
    pending = conn.execute(
        "SELECT count(*) AS n FROM plans pl JOIN request_revisions r ON r.change_id = pl.change_id "
        "AND r.revision_no = pl.revision_no JOIN template_versions t ON t.id = r.template_version_id "
        "WHERE t.slug = %s AND t.version = %s AND pl.status = 'ready'", (slug, version)).fetchone()["n"]
    return {"template": slug, "version": version, "projects": projects,
            "deployments": [{"id": str(d["id"]), "project": d["project"], "environment": d["environment"],
                             "pinned_version": d["version"]} for d in deployments],
            "plans_pending_on_version": pending,
            "note": "Publier ne modifie ni les plans approuvés ni les déploiements existants ; "
                    "les nouvelles demandes utiliseront la dernière version publiée."}


TRANSITIONS = {"publish": ({"validated"}, "published"),
               "deprecate": ({"published"}, "deprecated"),
               "withdraw": ({"published", "deprecated"}, "withdrawn")}


def change_status(conn: psycopg.Connection, actor_id: UUID, slug: str, version: str, action: str,
                  reason: str) -> dict[str, Any]:
    """Publier, déprécier ou retirer une version. Le droit est vérifié par l'appelant."""
    if action not in TRANSITIONS:
        raise ApiError(422, "invalid_action", "action attendue : publish, deprecate ou withdraw")
    if not reason or not reason.strip():
        raise ApiError(422, "reason_required", "un motif est obligatoire", field="/reason")
    row = conn.execute("SELECT * FROM template_versions WHERE slug = %s AND version = %s FOR UPDATE",
                       (slug, version)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "version de template introuvable")
    allowed, target = TRANSITIONS[action]
    if row["status"] not in allowed:
        raise ApiError(409, "invalid_status", f"statut actuel « {row['status']} » : action « {action} » impossible")
    effect = impact(conn, slug, version)
    if action == "publish":
        for comp in row["contract"]["components"]:
            c = component(conn, comp["component"], comp["version"])
            if c is None or c["status"] in ("withdrawn",):
                raise ApiError(409, "component_unavailable", f"composant {comp['component']}@{comp['version']} "
                               "absent ou retiré")
            if c["status"] == "validated":
                conn.execute("UPDATE component_versions SET status = 'published' WHERE id = %s", (c["id"],))
        conn.execute("UPDATE template_versions SET status = 'published', published_by = %s, published_at = now(), "
                     "publish_reason = %s WHERE id = %s", (actor_id, reason.strip(), row["id"]))
    else:
        conn.execute("UPDATE template_versions SET status = %s WHERE id = %s", (target, row["id"]))
    conn.execute("INSERT INTO template_events (template_version_id, action, actor_id, reason, impact) "
                 "VALUES (%s, %s, %s, %s, %s)",
                 (row["id"], {"publish": "published", "deprecate": "deprecated", "withdraw": "withdrawn"}[action],
                  actor_id, reason.strip(), Jsonb(effect)))
    audit.record(conn, actor_type="user" if actor_id else "system", actor_id=str(actor_id) if actor_id else "seed",
                 action=f"registry.template.{action}", outcome="ok",
                 targets={"template": slug, "projects": effect["projects"]},
                 versions={"template": version, "digest": row["digest"]},
                 policy_decision={"reason": reason.strip()})
    return {"template": slug, "version": version, "status": target, "impact": effect}


def publish_action(conn: psycopg.Connection, principal: Principal, slug: str, version: str, action: str,
                   reason: str) -> dict[str, Any]:
    """Point d'entrée des routes d'administration : publier n'est ni déployer ni approuver."""
    require_publisher(principal)
    return change_status(conn, principal.user_id, slug, version, action, reason)


def template_history(conn: psycopg.Connection, slug: str) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT e.id, t.version, e.action, e.reason, e.impact, e.at, u.display_name AS actor "
        "FROM template_events e JOIN template_versions t ON t.id = e.template_version_id "
        "LEFT JOIN users u ON u.id = e.actor_id WHERE t.slug = %s ORDER BY e.id", (slug,)).fetchall()
