"""Catalogue approuvé : templates Rackloom, registre de composants et éléments techniques.

- Un **composant** (registre) déclare OS pris en charge, prérequis, contrat de paramètres,
  dépendances, incompatibilités, effets attendus, implémentation et contrôles de santé.
- Un **template Rackloom** combine méthode de création, profil système, composants versionnés
  configurés, entrées fonctionnelles autorisées, règles de résolution et politique de déploiement.
  Ses versions publiées servent de profils de service au résolveur.
- Une image vSphere n'est qu'un point de départ (inventaire), jamais un template Rackloom.

Chaque élément porte l'empreinte SHA-256 de son contenu canonique. Une version publiée est
immuable : la recharger avec un autre contenu est refusé.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import psycopg
import yaml

from .. import audit
from ..canonical import digest, file_tree_digest
from ..db import Jsonb
from ..errors import ApiError
from ..ids import uuid7

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
CREATION_METHODS = {"vsphere_image"}  # "ubuntu_autoinstall" : non pris en charge au lot 1


class CatalogError(Exception):
    pass


def semver_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise CatalogError(f"{path} : document YAML attendu")
    return data


def _check_lockfile(module_dir: Path) -> list[dict[str, str]]:
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
    return providers


# --- Validation des contrats ------------------------------------------------------------

def validate_component(m: dict[str, Any]) -> None:
    for key in ("schema", "name", "version", "title", "supports", "parameters", "effects",
                "implementation", "health_checks"):
        if key not in m:
            raise CatalogError(f"composant : champ {key} manquant")
    if m["schema"] != "rackloom.component/v1" or not VERSION_RE.match(str(m["version"])):
        raise CatalogError("composant : schéma ou version invalide")
    for name, spec in m["parameters"].items():
        if spec.get("type") not in ("enum", "integer", "string", "boolean", "secret_ref"):
            raise CatalogError(f"composant {m['name']} : type du paramètre {name} non pris en charge")
        if "default" in spec and "values" in spec and spec["default"] not in spec["values"]:
            raise CatalogError(f"composant {m['name']} : défaut hors des valeurs de {name}")


def check_component_config(component: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Vérifie une configuration de composant contre son contrat. Renvoie les écarts lisibles."""
    problems = []
    params = component["parameters"]
    for key, value in config.items():
        spec = params.get(key)
        if spec is None:
            problems.append(f"paramètre « {key} » non pris en charge par {component['name']}@"
                            f"{component['version']} : proposer une extension du composant")
            continue
        if "values" in spec and value not in spec["values"]:
            problems.append(f"{component['name']}.{key} : valeur {value!r} hors du contrat {spec['values']}")
        if spec["type"] == "integer" and not isinstance(value, int):
            problems.append(f"{component['name']}.{key} : entier attendu")
        if spec["type"] == "secret_ref" and not (isinstance(value, str) and value.startswith("secret://")):
            problems.append(f"{component['name']}.{key} : référence de secret attendue, jamais une valeur")
    return problems


def validate_template(m: dict[str, Any]) -> None:
    for key in ("schema", "slug", "version", "title", "creation", "system", "components", "inputs",
                "resolution", "deployment_policy", "ansible"):
        if key not in m:
            raise CatalogError(f"template : champ {key} manquant")
    if m["schema"] != "rackloom.template/v1":
        raise CatalogError("template : schéma inconnu")
    if not VERSION_RE.match(str(m["version"])):
        raise CatalogError("template : version non conforme (x.y.z)")
    if m["creation"].get("method") not in CREATION_METHODS:
        raise CatalogError(f"template : méthode de création non prise en charge ({m['creation'].get('method')})")
    inputs = m["inputs"]
    if inputs["default_size"] not in inputs["sizes"]:
        raise CatalogError("template : taille par défaut inconnue")
    for size in inputs["sizes"].values():
        for field in ("label", "cpu", "memory_mb", "disk_gb"):
            if field not in size:
                raise CatalogError(f"template : taille sans {field}")
    q = inputs["quantity"]
    if not (1 <= q["min"] <= q["default"] <= q["max"] <= 10):
        raise CatalogError("template : bornes de quantité invalides")
    for usage in inputs["usages"].values():
        if "prod" in usage.get("environment_kinds", []) and not usage.get("explicit_environment_required"):
            raise CatalogError("template : un usage de production doit exiger un environnement explicite")
    for secret in m.get("secrets", []):
        if not str(secret.get("ref", "")).startswith("secret://"):
            raise CatalogError("template : les secrets sont des références secret://, jamais des valeurs")


# --- Lecture du dépôt -------------------------------------------------------------------

def scan(catalog_dir: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted((catalog_dir / "terraform").glob("*/*")):
        if path.is_dir():
            items.append({
                "kind": "tf_module", "slug": path.parent.name, "version": path.name,
                "digest": file_tree_digest(path, ("*.tf", ".terraform.lock.hcl")),
                "manifest": {"providers": _check_lockfile(path),
                             "lockfile_sha256": file_tree_digest(path, (".terraform.lock.hcl",))},
                "source_ref": f"catalog/terraform/{path.parent.name}/{path.name}",
            })
    for path in sorted((catalog_dir / "ansible" / "roles").glob("*/*")):
        meta = path / "meta" / "rackloom.yml"
        if not meta.exists():
            raise CatalogError(f"{path} : contrat de rôle meta/rackloom.yml absent")
        items.append({
            "kind": "ansible_role", "slug": path.parent.name, "version": path.name,
            "digest": file_tree_digest(path), "manifest": _read_yaml(meta),
            "source_ref": f"catalog/ansible/roles/{path.parent.name}/{path.name}",
        })
    for path in sorted((catalog_dir / "ansible" / "playbooks").glob("*/*")):
        items.append({
            "kind": "playbook", "slug": path.parent.name, "version": path.name,
            "digest": file_tree_digest(path), "manifest": {"file": f"{path.parent.name}.yml"},
            "source_ref": f"catalog/ansible/playbooks/{path.parent.name}/{path.name}",
        })
    for path in sorted((catalog_dir / "components").glob("*/*/component.yaml")):
        manifest = _read_yaml(path)
        validate_component(manifest)
        items.append({
            "kind": "component", "slug": manifest["name"], "version": manifest["version"],
            "digest": digest(manifest), "manifest": manifest,
            "source_ref": f"catalog/components/{manifest['name']}/{manifest['version']}",
        })
    for path in sorted((catalog_dir / "templates").glob("*/*/template.yaml")):
        manifest = _read_yaml(path)
        validate_template(manifest)
        if manifest["slug"] != path.parent.parent.name or manifest["version"] != path.parent.name:
            raise CatalogError(f"{path} : slug ou version incohérents avec le chemin")
        items.append({
            "kind": "template", "slug": manifest["slug"], "version": manifest["version"],
            "digest": digest(manifest), "manifest": manifest,
            "source_ref": f"catalog/templates/{manifest['slug']}/{manifest['version']}",
        })
    index = {(i["kind"], i["slug"], i["version"]): i for i in items}
    for item in items:
        if item["kind"] == "template":
            _check_template_references(item["manifest"], lambda k, s, v: index.get((k, s, v)))
        if item["kind"] == "component":
            role = item["manifest"]["implementation"]["ansible_role"]
            if ("ansible_role", role["name"], role["version"]) not in index:
                raise CatalogError(f"composant {item['slug']} : rôle {role} absent")
    return items


def _check_template_references(m: dict[str, Any], lookup) -> None:
    refs = [("tf_module", m["creation"]["terraform"]["module"], m["creation"]["terraform"]["version"])]
    refs += [("playbook", m["ansible"][k]["playbook"], m["ansible"][k]["version"]) for k in ("configure", "collect")]
    for ref in refs:
        if lookup(*ref) is None:
            raise CatalogError(f"template {m['slug']}@{m['version']} : référence absente {ref}")
    for comp in m["components"]:
        item = lookup("component", comp["name"], comp["version"])
        if item is None:
            raise CatalogError(f"template {m['slug']} : composant {comp['name']}@{comp['version']} absent")
        problems = check_component_config(item["manifest"], comp.get("config", {}))
        if problems:
            raise CatalogError("; ".join(problems))
        supported = item["manifest"]["supports"]["os"]
        os_ = m["system"]["os"]
        if not any(s["family"] == os_["family"] and os_["version"] in s["versions"] for s in supported):
            raise CatalogError(f"composant {comp['name']} incompatible avec {os_['family']} {os_['version']}")


def load(conn: psycopg.Connection, catalog_dir: Path) -> dict[str, int]:
    """Charge le catalogue du dépôt. Refuse toute version publiée dont le contenu aurait changé."""
    loaded = 0
    for item in scan(catalog_dir):
        row = get_item(conn, item["kind"], item["slug"], item["version"])
        if row:
            if row["digest"] != item["digest"]:
                raise CatalogError(f"{item['kind']} {item['slug']}@{item['version']} modifié après "
                                   "publication : publier une nouvelle version")
            continue
        conn.execute(
            """
            INSERT INTO catalog_items (id, kind, slug, version, digest, manifest, source_ref, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'chargement du catalogue du dépôt')
            """,
            (uuid7(), item["kind"], item["slug"], item["version"], item["digest"], Jsonb(item["manifest"]),
             item["source_ref"]),
        )
        loaded += 1
    return {"loaded": loaded}


# --- Requêtes ---------------------------------------------------------------------------

def get_item(conn: psycopg.Connection, kind: str, slug: str, version: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM catalog_items WHERE kind = %s AND slug = %s AND version = %s", (kind, slug, version)
    ).fetchone()


def latest_template(conn: psycopg.Connection, slug: str) -> dict[str, Any] | None:
    """Dernière version publiée (les versions dépréciées ou retirées ne sont plus proposées)."""
    rows = conn.execute(
        "SELECT * FROM catalog_items WHERE kind = 'template' AND slug = %s AND status = 'published'", (slug,)
    ).fetchall()
    return max(rows, key=lambda r: semver_key(r["version"])) if rows else None


def templates_for_project(conn: psycopg.Connection, project_id) -> list[dict[str, Any]]:
    slugs = [r["template_slug"] for r in conn.execute(
        "SELECT template_slug FROM project_templates WHERE project_id = %s ORDER BY template_slug",
        (project_id,)).fetchall()]
    return [t for t in (latest_template(conn, s) for s in slugs) if t is not None]


def template_versions(conn: psycopg.Connection, slug: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, slug, version, digest, status, reason, published_by, loaded_at, status_changed_at "
        "FROM catalog_items WHERE kind = 'template' AND slug = %s", (slug,)).fetchall()
    return sorted(rows, key=lambda r: semver_key(r["version"]), reverse=True)


def components_of(conn: psycopg.Connection, template_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for comp in template_manifest["components"]:
        item = get_item(conn, "component", comp["name"], comp["version"])
        if item is None:
            raise CatalogError(f"composant {comp['name']}@{comp['version']} absent du registre")
        result.append({"ref": comp, "item": item})
    return result


def health_checks(conn: psycopg.Connection, template_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    checks = []
    for comp in components_of(conn, template_manifest):
        for check in comp["item"]["manifest"]["health_checks"]:
            checks.append(dict(check, component=comp["ref"]["name"]))
    return checks


def publish_template_version(conn: psycopg.Connection, manifest: dict[str, Any], reason: str,
                             actor_id) -> dict[str, Any]:
    """Publication par un auteur habilité, hors du parcours de déploiement. Ne déploie rien."""
    try:
        validate_template(manifest)
        _check_template_references(manifest, lambda k, s, v: get_item(conn, k, s, v))
    except CatalogError as exc:
        raise ApiError(422, "invalid_template", str(exc)) from exc
    if not reason.strip():
        raise ApiError(422, "reason_required", "un motif de publication est obligatoire", field="/reason")
    slug, version = manifest["slug"], manifest["version"]
    if get_item(conn, "template", slug, version):
        raise ApiError(409, "version_exists", f"{slug}@{version} existe déjà : publier une nouvelle version")
    current = latest_template(conn, slug)
    if current and semver_key(version) <= semver_key(current["version"]):
        raise ApiError(409, "version_not_newer", f"la version doit être supérieure à {current['version']}")
    d = digest(manifest)
    conn.execute(
        """
        INSERT INTO catalog_items (id, kind, slug, version, digest, manifest, source_ref, published_by, reason)
        VALUES (%s, 'template', %s, %s, %s, %s, 'publication', %s, %s)
        """,
        (uuid7(), slug, version, d, Jsonb(manifest), actor_id, reason.strip()),
    )
    affected = [r["slug"] for r in conn.execute(
        "SELECT p.slug FROM project_templates pt JOIN projects p ON p.id = pt.project_id "
        "WHERE pt.template_slug = %s ORDER BY p.slug", (slug,)).fetchall()]
    audit.record(conn, actor_type="user", actor_id=str(actor_id), action="catalog.template.publish",
                 outcome="ok", targets={"template": slug, "affected_projects": affected},
                 versions={"template": version, "previous": current["version"] if current else None,
                           "digest": d},
                 policy_decision={"reason": reason.strip()})
    return {"slug": slug, "version": version, "digest": d, "affected_projects": affected,
            "previous_version": current["version"] if current else None}


def set_template_status(conn: psycopg.Connection, slug: str, version: str, status: str, reason: str,
                        actor_id) -> None:
    if status not in ("deprecated", "withdrawn"):
        raise ApiError(422, "invalid_status", "statut attendu : deprecated ou withdrawn")
    row = get_item(conn, "template", slug, version)
    if row is None:
        raise ApiError(404, "not_found", "version de template introuvable")
    conn.execute("UPDATE catalog_items SET status = %s, status_changed_at = now() WHERE id = %s",
                 (status, row["id"]))
    audit.record(conn, actor_type="user", actor_id=str(actor_id), action=f"catalog.template.{status}",
                 outcome="ok", targets={"template": slug}, versions={"template": version},
                 policy_decision={"reason": reason})
