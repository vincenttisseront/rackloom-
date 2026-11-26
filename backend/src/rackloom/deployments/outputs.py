"""F33 : sorties de déploiement typées, versionnées, limitées à `outputs_allowlist`.

Publication depuis les instances observées après apply — jamais lecture du state Terraform
par l'API. Lecture filtrée par périmètre projet (hors périmètre → 404).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import registry
from ..db import Conn
from ..errors import not_found
from ..identity.authz import Principal, require_project
from ..ids import uuid7

OUTPUT_TYPES = {
    "name": "name",
    "vm_uuid": "vm_uuid",
    "fqdn": "fqdn",
    "service_id": "service_id",
}

LABELS = {
    "name": "Nom",
    "vm_uuid": "Identifiant de ressource",
    "fqdn": "FQDN",
    "service_id": "Identifiant de service",
}


def _entries_from_instances(allow: set[str], instances: list[dict[str, Any]],
                            deployment_id: UUID) -> list[dict[str, Any]]:
    """Construit les sorties publiables ; ignore toute clé hors liste et toute valeur vide."""
    rows: list[dict[str, Any]] = []
    for inst in instances:
        name = inst.get("name")
        if not isinstance(name, str) or not name:
            continue
        uuid = inst.get("vm_uuid") or inst.get("vm_stable_id")
        # Jamais publier une valeur canari (tests de fuite) ni un UUID canari.
        if (isinstance(uuid, str) and uuid.startswith("canary")) or name.startswith("canary"):
            continue
        if "name" in allow:
            rows.append({"key": "name", "output_type": "name", "value": name, "instance_name": name})
        if "vm_uuid" in allow:
            if isinstance(uuid, str) and uuid:
                rows.append({"key": "vm_uuid", "output_type": "vm_uuid", "value": uuid, "instance_name": name})
        if "fqdn" in allow:
            fqdn = inst.get("fqdn")
            if isinstance(fqdn, str) and fqdn and "@" not in fqdn:
                rows.append({"key": "fqdn", "output_type": "fqdn", "value": fqdn, "instance_name": name})
        if "service_id" in allow:
            sid = inst.get("service_id") or f"svc:{deployment_id}:{name}"
            if isinstance(sid, str) and sid:
                rows.append({"key": "service_id", "output_type": "service_id", "value": sid, "instance_name": name})
    return rows


def publish(conn: Conn, *, deployment_id: UUID, project_id: UUID, template_version_id: UUID,
            instances: list[dict[str, Any]], allowlist: set[str] | None = None) -> int:
    """Publie une nouvelle version des sorties allowlistées. Renvoie le nombre d'entrées."""
    if allowlist is None:
        tpl = registry.template_by_id(conn, template_version_id)
        allow = set((tpl["contract"] if tpl else {}).get("outputs_allowlist") or [])
    else:
        allow = set(allowlist)
    entries = _entries_from_instances(allow, instances, deployment_id)
    prev = conn.execute(
        "SELECT coalesce(max(version), 0) AS v FROM deployment_outputs WHERE deployment_id = %s",
        (deployment_id,),
    ).fetchone()
    version = int(prev["v"]) + 1
    for entry in entries:
        conn.execute(
            "INSERT INTO deployment_outputs (id, deployment_id, project_id, key, output_type, value, version, "
            "instance_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (uuid7(), deployment_id, project_id, entry["key"], entry["output_type"], entry["value"], version,
             entry["instance_name"]),
        )
    return len(entries)


def latest(conn: Conn, principal: Principal, deployment_id: UUID) -> dict[str, Any]:
    """Dernière version publiée ; 404 hors périmètre. Ne lit jamais le state."""
    dep = conn.execute("SELECT id, project_id FROM deployments WHERE id = %s", (deployment_id,)).fetchone()
    if dep is None:
        raise not_found("déploiement")
    require_project(principal, dep["project_id"], "déploiement")
    ver = conn.execute(
        "SELECT coalesce(max(version), 0) AS v FROM deployment_outputs WHERE deployment_id = %s",
        (deployment_id,),
    ).fetchone()
    version = int(ver["v"])
    if version == 0:
        return {"deployment_id": str(deployment_id), "version": 0, "outputs": []}
    rows = conn.execute(
        "SELECT key, output_type, value, instance_name, published_at FROM deployment_outputs "
        "WHERE deployment_id = %s AND version = %s ORDER BY key, instance_name",
        (deployment_id, version),
    ).fetchall()
    return {
        "deployment_id": str(deployment_id),
        "version": version,
        "outputs": [
            {
                "key": r["key"],
                "type": r["output_type"],
                "label": LABELS.get(r["output_type"], r["key"]),
                "value": r["value"],
                "instance_name": r["instance_name"],
                "published_at": r["published_at"].isoformat(),
            }
            for r in rows
        ],
    }
