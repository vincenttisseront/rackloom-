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
publier. L'atelier (AT01) dépose une version `in_review` dont le contenu est déjà immuable.
La publication est une action d'administration distincte, auditée, avec motif ; elle ne crée
aucun job et ne modifie ni plan ni déploiement. Une version soumise par un auteur n'est pas
publiée par lui-même.

F01 : une source Git doit être épinglée (commit ou empreinte) et revue avant publication.
La promotion vers un kind d'environnement (`dev` / `test` / `preprod` / `prod`) est un acte
distinct : elle ne touche ni plan approuvé ni déploiement existant.

Le niveau de vérification d'un composant est calculé à partir des preuves enregistrées
(`component_evidence`), jamais déclaré dans le catalogue.
"""


from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from .. import audit
from ..canonical import digest, file_tree_digest
from ..db import Conn, Jsonb
from ..errors import ApiError, forbidden
from ..identity.authz import Principal, require_publisher
from ..ids import uuid7
from . import git_source

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.+_-]{0,62}$")


LOG_PATH_RE = re.compile(r"^/var/log/[a-z0-9][a-z0-9._/-]{0,120}$")


INPUT_NAMES = {"quantity", "size_tier", "usage", "lifetime_until"}


PARAM_TYPES = {"integer", "boolean", "enum", "string", "secret_ref"}


PARAM_KEYS = {"type", "default", "min", "max", "values", "label", "requires_component"}


CHECK_TYPES = {"package_installed", "service_active", "http_probe", "jail_active"}


SECRET_KEY_RE = re.compile(r"(pass(word)?|secret|token|private_key|api_key|credential)", re.I)


PLAYBOOKS = {"configure": "1.0.0", "collect": "1.1.0"}


LEGACY_LOGS = {"nginx": [{"name": "nginx", "path": "/var/log/nginx/error.log"}]}


WAVE_LIMITS = {"pilot_size": (1, 3), "wave_size": (1, 10)}


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
    _require(
        m,
        ("schema", "slug", "version", "title", "supports", "parameters", "effects", "checks", "implementation"),
        "composant",
    )
    if (
        m["schema"] != "rackloom.component/v1"
        or not VERSION_RE.match(str(m["version"]))
        or not SLUG_RE.match(str(m["slug"]))
    ):
        raise CatalogError("composant : schéma, slug ou version invalide")
    for name, spec in m["parameters"].items():
        if spec.get("type") not in PARAM_TYPES:
            raise CatalogError(f"composant {m['slug']} : type du paramètre {name} non pris en charge")
        if set(spec) - PARAM_KEYS:
            raise CatalogError(
                f"composant {m['slug']} : attribut inconnu pour {name} : {sorted(set(spec) - PARAM_KEYS)}"
            )
        if "default" in spec and check_parameter(spec, spec["default"]):
            raise CatalogError(f"composant {m['slug']} : défaut invalide pour {name}")
        if "requires_component" in spec and (
            spec["type"] != "boolean" or not SLUG_RE.match(str(spec["requires_component"]))
        ):
            raise CatalogError(f"composant {m['slug']} : requires_component réservé aux booléens ({name})")
    for check in m["checks"]:
        if check.get("type") not in CHECK_TYPES:
            raise CatalogError(f"composant {m['slug']} : contrôle « {check.get('type')} » non pris en charge")
        if "when" in check and m["parameters"].get(check["when"], {}).get("type") != "boolean":
            raise CatalogError(f"composant {m['slug']} : condition de contrôle sur un paramètre non booléen")
    for pkg in m["effects"].get("packages", []):
        if not NAME_RE.match(str(pkg)):
            raise CatalogError(f"composant {m['slug']} : nom de paquet invalide ({pkg})")
    for svc in m["effects"].get("services", []):
        if not NAME_RE.match(str(svc.get("unit", ""))) or not NAME_RE.match(str(svc.get("process", svc["unit"]))):
            raise CatalogError(f"composant {m['slug']} : service ou processus invalide")
    for log in m["effects"].get("logs", []):
        if (
            not NAME_RE.match(str(log.get("name", "")))
            or not LOG_PATH_RE.match(str(log.get("path", "")))
            or ".." in str(log.get("path"))
        ):
            raise CatalogError(f"composant {m['slug']} : journal déclaré invalide ({log})")
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


def check_waves(waves: dict[str, Any]) -> None:
    """`single` : toutes les cibles ensemble. `pilot` : un pilote, puis des vagues ; tout échec arrête la suite."""
    mode = waves.get("mode")
    if mode == "single" and set(waves) == {"mode"}:
        return
    if mode != "pilot" or set(waves) != {"mode", "pilot_size", "wave_size", "stop_on_failure"}:
        raise CatalogError(
            "template : vagues attendues « single » ou « pilot » (pilot_size, wave_size, stop_on_failure)"
        )
    for key, (low, high) in WAVE_LIMITS.items():
        value = waves[key]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise CatalogError(f"template : {key} hors bornes [{low}, {high}]")
    if waves["stop_on_failure"] is not True:
        raise CatalogError("template : un échec d'une vague arrête toujours les vagues suivantes")


def plan_waves(waves: dict[str, Any], names: list[str]) -> list[list[str]]:
    """Découpe ordonnée des cibles approuvées en vagues (le pilote d'abord)."""
    if not names:
        return []
    if waves.get("mode") != "pilot":
        return [list(names)]
    pilot, rest = list(names[: waves["pilot_size"]]), list(names[waves["pilot_size"] :])
    size = waves["wave_size"]
    return [pilot] + [rest[i : i + size] for i in range(0, len(rest), size)]


def component_checks(contract: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Contrôles de santé d'un composant configuré : paramètres substitués, conditions évaluées."""
    checks = []
    for check in contract["checks"]:
        if "when" in check and not config.get(check["when"]):
            continue
        item = {}
        for key, value in check.items():
            if key == "when":
                continue
            if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
                value = config[value[1:-1]]
            item[key] = value
        checks.append(item)
    return checks


def collect_spec(components: list[dict[str, Any]], health_checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Ce que la collecte en lecture seule interroge : paquets, processus et journaux déclarés."""
    packages: list[str] = []
    for c in components:
        packages += [p for p in c["effects"].get("packages", []) if p not in packages]
    for check in health_checks:
        if check["type"] == "package_installed" and check["package"] not in packages:
            packages.append(check["package"])
    processes = list(
        dict.fromkeys(str(c.get("process", c["unit"])) for c in health_checks if c["type"] == "service_active")
    )
    journals: list[dict[str, str]] = []
    for c in components:
        for log in c["effects"].get("logs") or LEGACY_LOGS.get(c["slug"], []):
            if log not in journals:
                journals.append({"name": log["name"], "path": log["path"]})
    return {"packages": packages, "processes": processes, "journals": journals}


def validate_template(m: dict[str, Any]) -> None:
    _require(
        m,
        (
            "schema",
            "slug",
            "version",
            "title",
            "description",
            "creation_method",
            "system_profile",
            "components",
            "inputs",
            "environments",
            "health_checks",
            "deployment_policy",
            "terraform",
            "outputs_allowlist",
        ),
        "template",
    )
    if m["schema"] != "rackloom.template/v1" or not SLUG_RE.match(str(m["slug"])):
        raise CatalogError("template : schéma ou slug invalide")
    if not VERSION_RE.match(str(m["version"])):
        raise CatalogError("template : version non conforme (x.y.z)")
    if m["creation_method"].get("kind") != "clone_image":
        raise CatalogError(
            f"template : méthode de création « {m['creation_method'].get('kind')} » non prise en charge au lot 1"
        )
    unknown = set(m["inputs"]) - INPUT_NAMES
    if unknown:
        raise CatalogError(f"template : entrée(s) non prise(s) en charge : {sorted(unknown)}")
    q = m["inputs"].get("quantity", {})
    if q and not (1 <= q["min"] <= q["default"] <= q["max"] <= 10):
        raise CatalogError("template : bornes de quantité invalides")
    tiers = set(m["inputs"].get("size_tier", {}).get("values", []))
    kinds = set(m["deployment_policy"].get("admissible_environment_kinds", []))
    check_waves(m["deployment_policy"].get("waves", {}))
    for check in m["health_checks"]:
        if check.get("type") not in CHECK_TYPES:
            raise CatalogError(f"template : contrôle de santé « {check.get('type')} » non pris en charge")
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
    anti = m["deployment_policy"].get("anti_affinity")
    if anti is not None:
        if not isinstance(anti, dict) or anti.get("scope") not in ("cluster",):
            raise CatalogError("template : anti_affinity.scope doit être « cluster »")
        if set(anti) - {"scope"}:
            raise CatalogError("template : anti_affinity n'admet que « scope »")
    for lic in m["deployment_policy"].get("licenses") or []:
        if not isinstance(lic, dict) or not lic.get("sku") or set(lic) - {"sku", "per_instance"}:
            raise CatalogError("template : licenses attend [{sku, per_instance?}]")
        if "per_instance" in lic and (not isinstance(lic["per_instance"], int) or lic["per_instance"] < 1):
            raise CatalogError("template : licenses.per_instance doit être un entier ≥ 1")
    # F33 : sorties typées publiables (jamais le state brut).
    if not set(m["outputs_allowlist"]) <= {"name", "vm_uuid", "fqdn", "service_id"}:
        raise CatalogError("template : sortie publiable non prise en charge")
    # F58 : actions courantes optionnelles (identifiants connus uniquement).
    known_actions = {"increase_capacity", "request_restart"}
    for action in m.get("service_actions") or []:
        if not isinstance(action, dict) or action.get("id") not in known_actions:
            raise CatalogError("template : service_actions.id doit être increase_capacity ou request_restart")
    # F59 : niveaux d'accès optionnels (id + label).
    for level in m.get("access_levels") or []:
        if not isinstance(level, dict) or not level.get("id"):
            raise CatalogError("template : access_levels attend [{id, label?}]")
        if not isinstance(level["id"], str) or len(level["id"]) > 40:
            raise CatalogError("template : access_levels.id invalide")
    for ref in m.get("secret_refs", []):
        if not str(ref).startswith("secret://"):
            raise CatalogError("template : les secrets sont des références secret://, jamais des valeurs")
    _reject_secret_literals(m["components"], f"template {m['slug']}")


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
            raise CatalogError(
                f"template {m['slug']}@{m['version']} : composant {ref['component']}@"
                f"{ref['version']} absent du registre"
            )
        problems = check_component_config(comp, ref.get("config", {}))
        if problems:
            raise CatalogError(f"template {m['slug']}@{m['version']} : " + "; ".join(problems))
        if os_ not in comp["supports"]["os"]:
            raise CatalogError(
                f"template {m['slug']}@{m['version']} : composant {comp['slug']} incompatible avec {os_}"
            )
        slugs.add(comp["slug"])
    for ref in m["components"]:
        comp = component_lookup(ref["component"], ref["version"])
        for other in comp.get("incompatible_with", []):
            if other in slugs:
                raise CatalogError(f"template {m['slug']} : {comp['slug']} incompatible avec {other}")
        for dep in comp.get("depends_on", []):
            if dep not in slugs:
                raise CatalogError(f"template {m['slug']} : {comp['slug']} dépend de {dep}")
        config = {k: v["default"] for k, v in comp["parameters"].items() if "default" in v}
        config.update(ref.get("config", {}))
        for name, spec in comp["parameters"].items():
            needed = spec.get("requires_component")
            if needed and config.get(name) and needed not in slugs:
                raise CatalogError(f"template {m['slug']} : {comp['slug']}.{name} exige le composant {needed}")


def load(conn: Conn, catalog_dir: Path, *, source_pin_id: UUID | None = None) -> dict[str, int]:
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
            "INSERT INTO catalog_artifacts (id, kind, slug, version, digest, lockfile_sha256, manifest, "
            "source_ref, source_pin_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (uuid7(), a["kind"], a["slug"], a["version"], a["digest"], a["lockfile_sha256"], Jsonb(a["manifest"]),
             a["source_ref"], source_pin_id))
        counts["artifacts"] += 1
    for c in found["components"]:
        row = component(conn, c["slug"], c["version"])
        if row:
            if row["digest"] != c["digest"]:
                raise CatalogError(f"composant {c['slug']}@{c['version']} modifié après chargement : "
                                   "publiez une nouvelle version")
            continue
        conn.execute(
            "INSERT INTO component_versions (id, slug, version, contract, implementation, digest, "
            "source_ref, source_pin_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (uuid7(), c["slug"], c["version"], Jsonb(c["contract"]), Jsonb(c["implementation"]), c["digest"],
             c["source_ref"], source_pin_id))
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
            "INSERT INTO template_versions (id, slug, version, contract, digest, source_ref, source_pin_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (tid, t["slug"], t["version"], Jsonb(t["contract"]), t["digest"], t["source_ref"], source_pin_id))
        reason = "chargement de catalog/" if source_pin_id is None else "chargement depuis source épinglée"
        conn.execute("INSERT INTO template_events (template_version_id, action, reason) "
                     "VALUES (%s, 'loaded', %s)", (tid, reason))
        counts["templates"] += 1
    return counts


def load_from_git(
    conn: Conn,
    repo_path: Path,
    commit: str,
    *,
    actor_id: UUID | None = None,
    catalog_subdir: str = "",
) -> dict[str, Any]:
    """Importe un catalogue depuis un dépôt Git local épinglé à un commit (non publié)."""
    import tempfile

    repo = git_source.resolve_local_repo(repo_path)
    sha = git_source.resolve_commit(repo, commit)
    pin = git_source.create_pin(
        conn,
        source_kind="git_commit",
        repo_label=str(repo),
        commit_sha=sha,
        actor_id=actor_id,
    )
    with tempfile.TemporaryDirectory(prefix="rackloom-catalog-") as tmp:
        root = Path(tmp)
        git_source.checkout_tree(repo, sha, root)
        catalog_dir = root / catalog_subdir if catalog_subdir else root
        if not (catalog_dir / "templates").is_dir() and (catalog_dir / "catalog" / "templates").is_dir():
            catalog_dir = catalog_dir / "catalog"
        counts = load(conn, catalog_dir, source_pin_id=pin["id"])
    audit.record(
        conn,
        actor_type="user" if actor_id else "system",
        actor_id=str(actor_id) if actor_id else "catalog-git",
        action="registry.catalog.git_import",
        outcome="ok",
        targets={"pin_id": str(pin["id"])},
        versions={"commit_sha": sha, **{k: str(v) for k, v in counts.items()}},
    )
    return {"pin": {"id": str(pin["id"]), "commit_sha": sha, "reviewed": False}, "counts": counts}


def artifact(conn: Conn, kind: str, slug: str, version: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM catalog_artifacts WHERE kind = %s AND slug = %s AND version = %s",
                        (kind, slug, version)).fetchone()


def component(conn: Conn, slug: str, version: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM component_versions WHERE slug = %s AND version = %s",
                        (slug, version)).fetchone()


def template(conn: Conn, slug: str, version: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM template_versions WHERE slug = %s AND version = %s",
                        (slug, version)).fetchone()


def template_by_id(conn: Conn, template_version_id: UUID) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM template_versions WHERE id = %s", (template_version_id,)).fetchone()


def latest_published(conn: Conn, slug: str) -> dict[str, Any] | None:
    rows = conn.execute("SELECT * FROM template_versions WHERE slug = %s AND status = 'published'",
                        (slug,)).fetchall()
    return max(rows, key=lambda r: semver_key(r["version"])) if rows else None


def latest_promoted(conn: Conn, slug: str, environment_kind: str) -> dict[str, Any] | None:
    """Dernière version publiée et promue pour un kind d'environnement (F01)."""
    if environment_kind not in ENV_KINDS:
        return None
    rows = conn.execute(
        """
        SELECT t.* FROM template_versions t
        JOIN template_promotions p ON p.template_version_id = t.id
        WHERE t.slug = %s AND t.status = 'published' AND p.environment_kind = %s
        """,
        (slug, environment_kind),
    ).fetchall()
    return max(rows, key=lambda r: semver_key(r["version"])) if rows else None


def promoted_kinds(conn: Conn, slug: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT p.environment_kind FROM template_promotions p
        JOIN template_versions t ON t.id = p.template_version_id
        WHERE t.slug = %s AND t.status = 'published'
        """,
        (slug,),
    ).fetchall()
    return {r["environment_kind"] for r in rows}


def list_promotions(conn: Conn, template_version_id: UUID) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            "SELECT environment_kind, promoted_at, reason, promoted_by FROM template_promotions "
            "WHERE template_version_id = %s ORDER BY environment_kind",
            (template_version_id,),
        ).fetchall()
    )


def promote(
    conn: Conn,
    principal: Principal,
    slug: str,
    version: str,
    environment_kind: str,
    reason: str,
) -> dict[str, Any]:
    """Promeut une version publiée vers un kind d'environnement. Ne crée aucun job."""
    require_publisher(principal)
    if environment_kind not in ENV_KINDS:
        raise ApiError(422, "invalid_environment_kind", f"kind attendu : {', '.join(ENV_KINDS)}")
    if not reason or not reason.strip():
        raise ApiError(422, "reason_required", "un motif est obligatoire", field="/reason")
    row = conn.execute(
        "SELECT * FROM template_versions WHERE slug = %s AND version = %s FOR UPDATE", (slug, version)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "version de template introuvable")
    if row["status"] != "published":
        raise ApiError(409, "not_published", "seule une version publiée peut être promue")
    admissible = set(row["contract"]["deployment_policy"].get("admissible_environment_kinds", []))
    if environment_kind not in admissible:
        raise ApiError(
            422,
            "environment_not_admissible",
            f"le template n'admet pas l'environnement « {environment_kind} »",
        )
    existing = conn.execute(
        "SELECT 1 FROM template_promotions WHERE template_version_id = %s AND environment_kind = %s",
        (row["id"], environment_kind),
    ).fetchone()
    if existing:
        raise ApiError(409, "already_promoted", f"déjà promue vers {environment_kind}")
    effect = impact(conn, slug, version)
    conn.execute(
        "INSERT INTO template_promotions (id, template_version_id, environment_kind, promoted_by, reason) "
        "VALUES (%s, %s, %s, %s, %s)",
        (uuid7(), row["id"], environment_kind, principal.user_id, reason.strip()),
    )
    conn.execute(
        "INSERT INTO template_events (template_version_id, action, actor_id, reason, impact) "
        "VALUES (%s, 'promoted', %s, %s, %s)",
        (row["id"], principal.user_id, f"{environment_kind}: {reason.strip()}", Jsonb(effect)),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="registry.template.promote",
        outcome="ok",
        targets={"template": slug, "environment_kind": environment_kind, "projects": effect["projects"]},
        versions={"template": version, "digest": row["digest"]},
        policy_decision={"reason": reason.strip()},
    )
    return {
        "template": slug,
        "version": version,
        "environment_kind": environment_kind,
        "status": "published",
        "impact": effect,
        "note": "La promotion ne modifie ni plan approuvé ni déploiement existant.",
    }


def granted_services(conn: Conn, project_id: UUID,
                     environment_id: UUID | None = None) -> list[str]:
    query = ("SELECT DISTINCT stable_id FROM resource_grants WHERE kind = 'service_template' AND project_id = %s "
             "AND revoked_at IS NULL")
    params: list[Any] = [project_id]
    if environment_id is not None:
        query += " AND environment_id = %s"
        params.append(environment_id)
    return [r["stable_id"] for r in conn.execute(query + " ORDER BY stable_id", params).fetchall()]


def template_components(conn: Conn, contract: dict[str, Any]) -> list[dict[str, Any]]:
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


def impact(conn: Conn, slug: str, version: str) -> dict[str, Any]:
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
            "note": "Publier ou promouvoir ne modifie ni les plans approuvés ni les déploiements "
                    "existants ; les nouvelles demandes utilisent la dernière version promue "
                    "pour l'environnement."}


TRANSITIONS = {
    "publish": ({"validated", "in_review"}, "published"),
    "return": ({"in_review"}, "returned"),
    "deprecate": ({"published"}, "deprecated"),
    "withdraw": ({"published", "deprecated"}, "withdrawn"),
}


EVENT_ACTIONS = {"publish": "published", "return": "returned", "deprecate": "deprecated", "withdraw": "withdrawn"}

ENV_KINDS = ("dev", "test", "preprod", "prod")


def change_status(
    conn: Conn, actor_id: UUID, slug: str, version: str, action: str, reason: str, digest_seen: str | None = None
) -> dict[str, Any]:
    """Publier, renvoyer, déprécier ou retirer une version. Le droit est vérifié par l'appelant.

    Une version soumise depuis l'atelier se publie sur l'empreinte affichée au relecteur, par une
    autre personne que l'auteur qui l'a soumise. Publier ne promeut pas (F01).
    """
    if action not in TRANSITIONS:
        raise ApiError(422, "invalid_action", "action attendue : publish, return, deprecate ou withdraw")
    if not reason or not reason.strip():
        raise ApiError(422, "reason_required", "un motif est obligatoire", field="/reason")
    row = conn.execute(
        "SELECT * FROM template_versions WHERE slug = %s AND version = %s FOR UPDATE", (slug, version)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "version de template introuvable")
    allowed, target = TRANSITIONS[action]
    if row["status"] not in allowed:
        raise ApiError(409, "invalid_status", f"statut actuel « {row['status']} » : action « {action} » impossible")
    if row["status"] == "in_review":
        if actor_id is not None and actor_id == row["submitted_by"]:
            raise forbidden(
                "un auteur ne publie ni ne renvoie sa propre version (séparation des droits)",
                code="separation_of_duties",
            )
        if digest_seen != row["digest"]:
            raise ApiError(
                409,
                "stale_digest",
                "l'empreinte affichée ne correspond pas à la version soumise : recharger avant de décider",
                field="/digest",
            )
    if action == "publish":
        git_source.require_publishable_pin(conn, row.get("source_pin_id"))
    effect = impact(conn, slug, version)
    if action == "return":
        conn.execute("UPDATE template_versions SET status = 'returned' WHERE id = %s", (row["id"],))
        if row["draft_id"]:
            conn.execute(
                "UPDATE template_drafts SET status = 'editing', version = %s, template_version_id = NULL, "
                "updated_at = now(), row_version = row_version + 1 WHERE id = %s",
                (next_patch(version), row["draft_id"]),
            )
    elif action == "publish":
        for comp in row["contract"]["components"]:
            c = component(conn, comp["component"], comp["version"])
            if c is None or c["status"] in ("withdrawn",):
                raise ApiError(
                    409, "component_unavailable", f"composant {comp['component']}@{comp['version']} absent ou retiré"
                )
            if c["status"] == "validated":
                conn.execute("UPDATE component_versions SET status = 'published' WHERE id = %s", (c["id"],))
        conn.execute(
            "UPDATE template_versions SET status = 'published', published_by = %s, published_at = now(), "
            "publish_reason = %s WHERE id = %s",
            (actor_id, reason.strip(), row["id"]),
        )
        if row["draft_id"]:
            conn.execute(
                "UPDATE template_drafts SET status = 'published', updated_at = now(), "
                "row_version = row_version + 1 WHERE id = %s",
                (row["draft_id"],),
            )
        # F01 : catalogue filesystem (appliance) → promotion initiale automatique vers les kinds
        # admissibles. Une source Git/artefact exige une promotion explicite après publication.
        pin = None
        if row.get("source_pin_id"):
            pin = conn.execute(
                "SELECT source_kind FROM catalog_source_pins WHERE id = %s", (row["source_pin_id"],)
            ).fetchone()
        if pin is None or pin["source_kind"] == "filesystem":
            for kind in row["contract"]["deployment_policy"].get("admissible_environment_kinds", []):
                if kind not in ENV_KINDS:
                    continue
                conn.execute(
                    "INSERT INTO template_promotions (id, template_version_id, environment_kind, promoted_by, reason) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (
                        uuid7(),
                        row["id"],
                        kind,
                        actor_id,
                        "promotion initiale à la publication (catalogue appliance)",
                    ),
                )
    else:
        conn.execute("UPDATE template_versions SET status = %s WHERE id = %s", (target, row["id"]))
    conn.execute(
        "INSERT INTO template_events (template_version_id, action, actor_id, reason, impact) "
        "VALUES (%s, %s, %s, %s, %s)",
        (row["id"], EVENT_ACTIONS[action], actor_id, reason.strip(), Jsonb(effect)),
    )
    if row["draft_id"]:
        conn.execute(
            "INSERT INTO template_draft_events (draft_id, action, actor_type, actor_id, data, content_digest) "
            "VALUES (%s, %s, 'user', %s, %s, %s)",
            (
                row["draft_id"],
                EVENT_ACTIONS[action],
                str(actor_id) if actor_id else None,
                Jsonb({"version": version, "reason": reason.strip()}),
                row["digest"],
            ),
        )
    audit.record(
        conn,
        actor_type="user" if actor_id else "system",
        actor_id=str(actor_id) if actor_id else "seed",
        action=f"registry.template.{action}",
        outcome="ok",
        targets={"template": slug, "projects": effect["projects"]},
        versions={"template": version, "digest": row["digest"]},
        policy_decision={"reason": reason.strip()},
    )
    return {"template": slug, "version": version, "status": target, "impact": effect}


def publish_action(
    conn: Conn, principal: Principal, slug: str, version: str, action: str, reason: str, digest_seen: str | None = None
) -> dict[str, Any]:
    """Point d'entrée des routes d'administration : publier n'est ni déployer ni approuver."""
    require_publisher(principal)
    return change_status(conn, principal.user_id, slug, version, action, reason, digest_seen)


def next_patch(version: str) -> str:
    major, minor, patch = semver_key(version)
    return f"{major}.{minor}.{patch + 1}"


LEVELS = ("not_tested", "tested_locally", "lab_validated")


def record_evidence(
    conn: Conn,
    component_version_id: UUID,
    *,
    level: str,
    job_id: UUID | None,
    deployment_id: UUID | None,
    host_id: UUID | None,
    connector_mode: str,
    checks: list[dict[str, Any]],
) -> str:
    """Enregistre une preuve et recalcule le niveau de vérification (le plus élevé atteint)."""
    conn.execute(
        "INSERT INTO component_evidence (component_version_id, level, source, job_id, deployment_id, "
        "host_id, connector_mode, checks) VALUES (%s, %s, 'verify_job', %s, %s, %s, %s, %s)",
        (component_version_id, level, job_id, deployment_id, host_id, connector_mode, Jsonb(checks)),
    )
    best = max(
        (
            r["level"]
            for r in conn.execute(
                "SELECT DISTINCT level FROM component_evidence WHERE component_version_id = %s", (component_version_id,)
            ).fetchall()
        ),
        key=LEVELS.index,
    )
    conn.execute(
        "UPDATE component_versions SET verification_level = %s WHERE id = %s AND verification_level <> %s",
        (best, component_version_id, best),
    )
    return best


def evidence_summary(conn: Conn, component_version_id: UUID) -> dict[str, Any]:
    """Résumé sans donnée de projet : nombre de preuves, dernier niveau et date."""
    row = conn.execute(
        "SELECT count(*) AS n, max(recorded_at) AS last FROM component_evidence WHERE component_version_id = %s",
        (component_version_id,),
    ).fetchone()
    return {"count": row["n"], "last_at": row["last"].isoformat() if row["last"] else None}


def template_history(conn: Conn, slug: str) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT e.id, t.version, e.action, e.reason, e.impact, e.at, u.display_name AS actor "
        "FROM template_events e JOIN template_versions t ON t.id = e.template_version_id "
        "LEFT JOIN users u ON u.id = e.actor_id WHERE t.slug = %s ORDER BY e.id", (slug,)).fetchall()
