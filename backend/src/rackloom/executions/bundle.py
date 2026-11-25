"""Dossier d'exécution : document canonique dont le SHA-256 est l'empreinte approuvée.

Il fige tout ce qui conditionne l'exécution : révision de la demande et sa résolution,
template et composants (versions et empreintes), module Terraform et lockfile, plan binaire,
state au moment du plan, rôles et playbooks Ansible, images runner, vérifications, politique.
L'admission le recalcule depuis l'état courant ; toute différence bloque l'exécution.
"""

from __future__ import annotations

from typing import Any

import psycopg

from .. import catalog, policy
from ..canonical import digest


class BundleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def runner_images(conn: psycopg.Connection) -> dict[str, str]:
    return {r["job_kind"]: r["image_digest"] for r in conn.execute(
        "SELECT job_kind, image_digest FROM runner_images ORDER BY job_kind").fetchall()}


def _pinned_catalog(conn: psycopg.Connection, resolution: dict[str, Any]) -> dict[str, Any]:
    """Relit les éléments épinglés par la résolution et vérifie qu'ils restent utilisables."""
    tpl = resolution["template"]
    row = catalog.get_item(conn, "template", tpl["slug"], tpl["version"])
    if row is None or row["digest"] != tpl["digest"]:
        raise BundleError("template_changed", "le template épinglé a changé ou a disparu")
    if row["status"] == "withdrawn":
        raise BundleError("template_withdrawn",
                          f"le template {tpl['slug']}@{tpl['version']} a été retiré : nouvelle demande requise")
    pinned = {"template": {"slug": tpl["slug"], "version": tpl["version"], "digest": row["digest"]}}
    comps = []
    for comp in resolution["components"]:
        item = catalog.get_item(conn, "component", comp["name"], comp["version"])
        if item is None or item["digest"] != comp["digest"]:
            raise BundleError("component_changed", f"composant {comp['name']}@{comp['version']} modifié")
        comps.append({"name": comp["name"], "version": comp["version"], "digest": item["digest"],
                      "config": comp["config"]})
    pinned["components"] = comps
    tf = resolution["terraform"]
    tf_item = catalog.get_item(conn, "tf_module", tf["module"], tf["version"])
    if tf_item is None or tf_item["digest"] != tf["digest"]:
        raise BundleError("module_changed", "module Terraform épinglé modifié")
    pinned["terraform"] = {"module": tf["module"], "version": tf["version"], "digest": tf_item["digest"],
                           "lockfile_sha256": tf_item["manifest"]["lockfile_sha256"]}
    ans = resolution["ansible"]
    for role in ans["roles"]:
        item = catalog.get_item(conn, "ansible_role", role["name"], role["version"])
        if item is None or item["digest"] != role["digest"]:
            raise BundleError("role_changed", f"rôle {role['name']}@{role['version']} modifié")
    for key in ("configure", "collect"):
        pb = ans[key]
        item = catalog.get_item(conn, "playbook", pb["playbook"], pb["version"])
        if item is None or item["digest"] != pb["digest"]:
            raise BundleError("playbook_changed", f"playbook {pb['playbook']}@{pb['version']} modifié")
    pinned["ansible"] = {"roles": ans["roles"], "configure": ans["configure"], "collect": ans["collect"]}
    return pinned


def build(conn: psycopg.Connection, *, change: dict[str, Any], version: dict[str, Any], stack: dict[str, Any],
          plan_sha256: str, state_lineage: str | None, state_serial: int | None, valid_until: str,
          terraform_version: str | None) -> dict[str, Any]:
    resolution = version["resolution"]
    pinned = _pinned_catalog(conn, resolution)
    images = runner_images(conn)
    connectors = {r["slug"]: {"mode": r["mode"], "config_version": r["config_version"]} for r in conn.execute(
        "SELECT slug, mode, config_version FROM connectors ORDER BY slug").fetchall()}
    return {
        "schema": "rackloom.execution-bundle/v1",
        "change_id": str(change["id"]),
        "project_id": str(change["project_id"]),
        "environment": resolution["environment"],
        "request": {"version_no": version["version_no"], "content_sha256": version["content_sha256"],
                    "resolution_sha256": version["resolution_sha256"]},
        "targets": {"stack": stack["slug"], "vms": [vm["name"] for vm in resolution["vms"]],
                    "image": resolution["image"]["stable_id"], "network": resolution["network"]["stable_id"],
                    "placement": {k: (v or {}).get("stable_id") for k, v in resolution["placement"].items()},
                    "connectors": connectors},
        **pinned,
        "plan": {"sha256": plan_sha256, "state_lineage": state_lineage, "state_serial": state_serial,
                 "valid_until": valid_until, "terraform_version": terraform_version},
        "runner_images": images,
        "verification": resolution["verification"],
        "policy": resolution["policy"],
    }


def recompute_digest(conn: psycopg.Connection, plan: dict[str, Any], change: dict[str, Any],
                     version: dict[str, Any], stack: dict[str, Any], plan_sha256: str) -> str:
    current = build(conn, change=change, version=version, stack=stack, plan_sha256=plan_sha256,
                    state_lineage=plan["state_lineage"], state_serial=plan["state_serial"],
                    valid_until=plan["bundle"]["plan"]["valid_until"],
                    terraform_version=plan["bundle"]["plan"]["terraform_version"])
    active = policy.active()
    if current["policy"] != active:
        raise BundleError("policy_changed", "le bundle de politiques actif a changé depuis la résolution")
    return digest(current)
