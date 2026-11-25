"""Dossier d'exécution : document canonique dont le SHA-256 est l'empreinte approuvée.

Il fige tout ce qui conditionne l'exécution (docs/architecture.md §10) : version du besoin et
révision résolue, projet, environnement et version de sa politique, template et composants,
module Terraform et lockfile, plan binaire et state au moment du plan, rôles, playbooks et
variables générées, images runner, contrôles de santé, bundle de politiques et validité.
L'admission le recalcule depuis l'état courant ; toute différence bloque l'exécution.
"""


from __future__ import annotations

from typing import Any

from .. import policy, registry
from ..canonical import digest
from ..db import Conn


class BundleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def runner_images(conn: Conn) -> dict[str, str]:
    return {r["job_kind"]: r["image_digest"] for r in conn.execute(
        "SELECT job_kind, image_digest FROM runner_images ORDER BY job_kind").fetchall()}


def ansible_components(resolution: dict[str, Any]) -> list[dict[str, Any]]:
    """Variables Ansible : uniquement la configuration déclarée des composants du template."""
    return [{"slug": c["slug"], "version": c["version"], "role": c["role"]["name"], "params": c["config"]}
            for c in resolution["components"]]


def tf_variables(resolution: dict[str, Any], deployment_ref: str, lab_delay_seconds: int) -> dict[str, Any]:
    """Variables Terraform : générées depuis la révision, jamais fournies par un utilisateur."""
    placement = resolution["placement"]
    return {
        "deployment_id": deployment_ref,
        "lab_delay_seconds": lab_delay_seconds,
        "instances": [{"name": i["name"], "size_tier": i["size_tier"], "vcpu": i["vcpu"],
                       "memory_mb": i["memory_mb"], "disk_gb": i["disk_gb"],
                       "image_ref": placement["image"]["stable_id"],
                       "network_ref": placement["network"]["stable_id"]} for i in resolution["instances"]],
    }


def _pinned(conn: Conn, resolution: dict[str, Any]) -> dict[str, Any]:
    """Relit les éléments épinglés et vérifie que leur contenu n'a pas changé."""
    tpl = resolution["template"]
    row = registry.template(conn, tpl["slug"], tpl["version"])
    if row is None or row["digest"] != tpl["digest"]:
        raise BundleError("template_changed", "le template épinglé a changé ou a disparu")
    comps = []
    for c in resolution["components"]:
        item = registry.component(conn, c["slug"], c["version"])
        if item is None or item["digest"] != c["digest"]:
            raise BundleError("component_changed", f"composant {c['slug']}@{c['version']} modifié")
        role = registry.artifact(conn, "ansible_role", c["role"]["name"], c["role"]["version"])
        if role is None or role["digest"] != c["role"]["digest"]:
            raise BundleError("role_changed", f"rôle {c['role']['name']}@{c['role']['version']} modifié")
        comps.append({"slug": c["slug"], "version": c["version"], "digest": item["digest"], "config": c["config"],
                      "role": c["role"]})
    tf = resolution["terraform"]
    module = registry.artifact(conn, "tf_module", tf["module"], tf["version"])
    if module is None or module["digest"] != tf["digest"]:
        raise BundleError("module_changed", "module Terraform épinglé modifié")
    playbooks = {}
    for name, pb in resolution["ansible"]["playbooks"].items():
        item = registry.artifact(conn, "playbook", name, pb["version"])
        if item is None or item["digest"] != pb["digest"]:
            raise BundleError("playbook_changed", f"playbook {name}@{pb['version']} modifié")
        playbooks[name] = {"version": pb["version"], "digest": item["digest"]}
    return {"template": {"slug": tpl["slug"], "version": tpl["version"], "digest": row["digest"]},
            "components": comps,
            "terraform": {"module": tf["module"], "version": tf["version"], "digest": module["digest"],
                          "lockfile_sha256": module["lockfile_sha256"]},
            "ansible": {"playbooks": playbooks,
                        "variables_sha256": digest({"rackloom_components": ansible_components(resolution)})}}


def build(
    conn: Conn,
    *,
    change: dict[str, Any],
    version: dict[str, Any],
    revision: dict[str, Any],
    stack: dict[str, Any],
    plan_sha256: str,
    state_lineage: str | None,
    state_serial: int | None,
    valid_until: str,
    terraform_version: str | None,
) -> dict[str, Any]:
    resolution = revision["resolved"]
    connectors = {
        r["slug"]: {"mode": r["mode"], "config_version": r["config_version"]}
        for r in conn.execute("SELECT slug, mode, config_version FROM connectors ORDER BY slug").fetchall()
    }
    env = resolution["environment"]
    return {
        "schema": "rackloom.execution-bundle/v1",
        "change_id": str(change["id"]),
        "project_id": str(change["project_id"]),
        "request": {
            "version_no": version["version_no"],
            "need_sha256": version["need_sha256"],
            "revision_no": revision["revision_no"],
            "resolution_digest": revision["resolution_digest"],
        },
        "environment": {
            "id": env["id"],
            "slug": env["slug"],
            "is_production": env["is_production"],
            "policy_version": env["policy_version"],
            "policy_digest": env["policy_digest"],
        },
        "targets": {
            "deployment": str(change["deployment_id"]),
            "stack": stack["slug"],
            "instances": [i["name"] for i in resolution["instances"]],
            "waves": resolution.get("waves") or [[i["name"] for i in resolution["instances"]]],
            "placement": {k: v["stable_id"] for k, v in resolution["placement"].items() if isinstance(v, dict)},
            "connectors": connectors,
        },
        **_pinned(conn, resolution),
        "plan": {
            "sha256": plan_sha256,
            "state_lineage": state_lineage,
            "state_serial": state_serial,
            "valid_until": valid_until,
            "terraform_version": terraform_version,
        },
        "runner_images": runner_images(conn),
        "health_checks": resolution["health_checks"],
        "policy_bundle": resolution["policy_bundle"],
    }


def recompute_digest(conn: Conn, plan: dict[str, Any], change: dict[str, Any],
                     version: dict[str, Any], revision: dict[str, Any], stack: dict[str, Any],
                     plan_sha256: str) -> str:
    current = build(conn, change=change, version=version, revision=revision, stack=stack, plan_sha256=plan_sha256,
                    state_lineage=plan["state_lineage"], state_serial=plan["state_serial"],
                    valid_until=plan["bundle"]["plan"]["valid_until"],
                    terraform_version=plan["bundle"]["plan"]["terraform_version"])
    if current["policy_bundle"] != policy.active():
        raise BundleError("policy_changed", "le bundle de politiques actif a changé depuis la résolution")
    return digest(current)
