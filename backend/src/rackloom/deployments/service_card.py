"""F57 : fiche « Votre service est prêt ».

État du service calculé côté serveur (contrôles de santé + observation), jamais d'après
le seul succès d'un job ni un résumé de modèle. Sorties limitées à `outputs_allowlist`.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from .. import registry
from ..db import Conn
from ..errors import not_found
from ..identity.authz import Principal, require_project
from ..redact import Redactor

_URL_WITH_USERINFO = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/]+@[^\s]+")
_BARE_URL = re.compile(r"(?i)\bhttps?://[^\s]+")


def _scrub(value: str) -> str:
    """Masque secrets connus, URL avec identifiants, et URL brutes hors allowlist d'affichage."""
    text = Redactor().text(value)
    text = _URL_WITH_USERINFO.sub("[secret masqué]", text)
    text = _BARE_URL.sub("[lien masqué]", text)
    return text


STATUS_HEADLINES = {
    "ready": "Votre service est prêt",
    "created_not_ready": "Service créé, mais pas prêt",
    "failed": "Échec du service",
    "verification_unavailable": "Vérification indisponible",
    "unknown": "Service pas encore vérifié",
}

STATUS_LABELS = {
    "ready": "Prêt",
    "created_not_ready": "Créé, pas prêt",
    "failed": "Échec",
    "verification_unavailable": "Vérification indisponible",
    "unknown": "Non vérifié",
}

CHECK_LABELS = {
    "package_installed": "Paquet installé",
    "service_active": "Service actif",
    "http_probe": "Sonde HTTP",
    "jail_active": "Jail Fail2ban active",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _access_mode(contract: dict[str, Any]) -> str:
    ports = (contract.get("system_profile") or {}).get("security", {}).get("open_ports") or []
    http = [p for p in ports if p in (80, 443, 8080)]
    if 443 in http:
        return "HTTPS (port 443)"
    if 80 in http or 8080 in http:
        port = 80 if 80 in http else 8080
        return f"HTTP (port {port})"
    if ports:
        return "Ports ouverts : " + ", ".join(str(p) for p in ports)
    return "Accès selon le profil de service"


def _published_outputs(contract: dict[str, Any], instances: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Uniquement les sorties de la liste blanche ; jamais d'adresse ni de secret."""
    allow = set(contract.get("outputs_allowlist") or [])
    out: list[dict[str, str]] = []
    if "name" in allow:
        for inst in instances:
            name = inst.get("name")
            if isinstance(name, str) and name:
                out.append({"key": "name", "label": "Nom", "value": name})
    # vm_uuid : identifiant technique — affiché seulement s'il est dans la liste et présent,
    # jamais une URL ni un secret. Les UUID plantés de test restent des valeurs de labo.
    if "vm_uuid" in allow:
        for inst in instances:
            uuid = inst.get("vm_uuid") or inst.get("vm_stable_id")
            if isinstance(uuid, str) and uuid and not uuid.startswith("canary"):
                out.append({"key": "vm_uuid", "label": "Identifiant de ressource", "value": uuid})
    return out


def _flatten_checks(service_checks: list[dict[str, Any]], checked_at: str | None) -> dict[str, Any]:
    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for host_block in service_checks or []:
        host = host_block.get("host") or host_block.get("name") or "?"
        for check in host_block.get("checks") or []:
            entry = {
                "host": host,
                "type": check.get("type"),
                "label": CHECK_LABELS.get(str(check.get("type")), str(check.get("type"))),
                "evidence": _scrub(str(check.get("evidence") or "")),
                "checked_at": checked_at,
            }
            if check.get("ok"):
                succeeded.append(entry)
            else:
                entry["failure"] = _scrub(str(check.get("failure") or "contrôle en échec"))
                failed.append(entry)
        if host_block.get("reason") and not host_block.get("checks"):
            failed.append(
                {
                    "host": host,
                    "type": host_block.get("state") or "unavailable",
                    "label": "Observation",
                    "evidence": _scrub(str(host_block["reason"])),
                    "failure": _scrub(str(host_block["reason"])),
                    "checked_at": checked_at,
                }
            )
    return {"succeeded": succeeded, "failed": failed}


def build(conn: Conn, dep: dict[str, Any], *, principal: Principal | None = None) -> dict[str, Any]:
    """Construit la fiche courte (premier niveau) et les détails techniques (second niveau)."""
    tpl = registry.template_by_id(conn, dep["template_version_id"])
    contract = tpl["contract"]
    env = conn.execute(
        "SELECT slug, name, is_production FROM environments WHERE id = %s", (dep["environment_id"],)
    ).fetchone()
    project = conn.execute("SELECT slug, name FROM projects WHERE id = %s", (dep["project_id"],)).fetchone()
    owner = conn.execute("SELECT display_name FROM users WHERE id = %s", (dep["owner_id"],)).fetchone()
    status = dep["service_status"]
    checked_at = _iso(dep["service_checked_at"])
    checks = _flatten_checks(dep.get("service_checks") or [], checked_at)
    reason = dep.get("service_reason") or ""
    # Titres utilisateur : pour created_not_ready, le motif exact (ex. sonde HTTP) reste visible.
    detail_line = reason
    if status == "created_not_ready" and "sonde HTTP" in reason:
        motif = reason.removeprefix("VM créée, ").strip()
        headline_detail = reason if reason.startswith("Service") else f"Créé mais pas prêt : {motif}"
    elif status == "verification_unavailable":
        headline_detail = "Vérification indisponible"
    else:
        headline_detail = reason

    card: dict[str, Any] = {
        "deployment_id": str(dep["id"]),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "headline": STATUS_HEADLINES.get(status, STATUS_HEADLINES["unknown"]),
        "detail": headline_detail or detail_line,
        "service": {
            "title": contract["title"],
            "slug": tpl["slug"],
            "version": tpl["version"],
        },
        "environment": {
            "slug": env["slug"],
            "name": env["name"],
            "is_production": env["is_production"],
        },
        "project": {"slug": project["slug"], "name": project["name"]},
        "owner": owner["display_name"] if owner else None,
        "access_mode": _access_mode(contract),
        "outputs": _published_outputs(contract, list(dep.get("instances") or [])),
        "checked_at": checked_at,
        "checks": {
            "succeeded": checks["succeeded"],
            "failed": checks["failed"],
            "summary": (
                f"{len(checks['succeeded'])} réussie(s)"
                + (f", {len(checks['failed'])} en échec" if checks["failed"] else "")
            ),
        },
        "technical": {
            "deployment_status": dep["status"],
            "service_reason": reason,
            "hosts": [
                {
                    "id": str(h["id"]),
                    "name": h["name"],
                    "deployment_state": h["deployment_state"],
                }
                for h in conn.execute(
                    "SELECT id, name, deployment_state FROM hosts WHERE deployment_id = %s ORDER BY name",
                    (dep["id"],),
                ).fetchall()
            ],
            "note": "Détails techniques : états machine et motif serveur. Aucune adresse, aucun secret.",
        },
        "notice": "L'état du service vient des contrôles de santé observés, pas du seul succès d'un job.",
    }
    # F58 / F59 : actions et demande d'accès (filtrées si un principal est fourni).
    if principal is not None:
        from .. import access
        from . import actions as service_actions

        card["service_actions"] = service_actions.describe_for_card(conn, principal, dep)
        card["access_request"] = access.card_section(conn, principal, dep)
    else:
        card["service_actions"] = []
        card["access_request"] = None
    return card


def for_principal(conn: Conn, principal: Principal, deployment_id: UUID) -> dict[str, Any]:
    dep = conn.execute("SELECT * FROM deployments WHERE id = %s", (deployment_id,)).fetchone()
    if dep is None:
        raise not_found("déploiement")
    require_project(principal, dep["project_id"], "déploiement")
    return build(conn, dep, principal=principal)
