"""Connecteur Rudder fictif (RAC-36) — lecture seule, observations explicites.

Sans laboratoire Rudder réel (D2) : les rapports sont des fixtures marquées
`simulated=true` / source `rudder_fake`. Rudder ne remplace ni le résolveur ni
l'approbation ; aucune remédiation Rudder n'est déclenchée ici.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import audit, hosts
from ..db import Conn
from ..errors import ApiError, not_found
from ..identity.authz import Principal, require_role

SOURCE = "rudder_fake"
NOTICE = (
    "Observations fictives explicites (connecteur Rudder optionnel, lecture seule). "
    "Aucun laboratoire Rudder réel (D2 / RAC-36)."
)

# Rapport de démo : nginx présent mais server_tokens encore « on » → écart.
DEFAULT_REPORT: dict[str, Any] = {
    "node_id": "rudder-node-demo-001",
    "simulated": True,
    "packages": {"nginx": "1.24.0-2ubuntu7"},
    "services": {"nginx": {"active": True, "manager": "systemd", "systemd": "active"}},
    "settings": {"nginx.server_tokens": "on"},
}


def capabilities() -> dict[str, dict[str, Any]]:
    from ..connectors import cap

    return {
        "configure": cap("real", "URL et jeton (référence de secret) — non utilisé en fictif"),
        "test_connection": cap("simulated", "aucun appel réseau"),
        "inventory": cap("simulated", "rapports de conformité fictifs → observations"),
        "plan": cap(None, "remédiation Rudder hors périmètre F24"),
        "execute": cap(None, "aucune écriture via Rudder"),
        "track": cap(None),
        "cancel": cap(None),
    }


def import_observations(
    conn: Conn,
    principal: Principal,
    host_id: UUID,
    *,
    report: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Importe un rapport Rudder fictif en observations hôte (lecture seule)."""
    host = conn.execute("SELECT * FROM hosts WHERE id = %s", (host_id,)).fetchone()
    if host is None or not principal.can_see_project(host["project_id"]):
        raise not_found("machine")
    require_role(principal, "operator", host["project_id"], host["environment_id"])

    payload = dict(report) if isinstance(report, dict) else dict(DEFAULT_REPORT)
    if payload.get("simulated") is not True:
        # Force the explicit fiction marker — never present fictional data as real.
        payload["simulated"] = True

    kinds_written: list[str] = []
    if "packages" in payload and isinstance(payload["packages"], dict):
        hosts.observe(
            conn,
            host,
            "packages",
            {**payload["packages"], "_provenance": {"source": SOURCE, "simulated": True}},
            "ok",
            SOURCE,
            None,
        )
        kinds_written.append("packages")
    if "services" in payload and isinstance(payload["services"], dict):
        hosts.observe(conn, host, "services", payload["services"], "ok", SOURCE, None)
        kinds_written.append("services")
    if "settings" in payload and isinstance(payload["settings"], dict):
        hosts.observe(
            conn,
            host,
            "settings",
            {
                **payload["settings"],
                "_provenance": {"source": SOURCE, "simulated": True, "notice": NOTICE},
            },
            "ok",
            SOURCE,
            None,
        )
        kinds_written.append("settings")

    if not kinds_written:
        raise ApiError(422, "empty_report", "rapport Rudder fictif sans paquets/services/réglages")

    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="rudder_fake.import_observations",
        outcome="ok",
        project_id=host["project_id"],
        targets={"host_id": str(host_id), "kinds": ",".join(kinds_written)},
        versions={"simulated": "true"},
        request_id=request_id,
    )
    return {
        "host_id": str(host_id),
        "source": SOURCE,
        "kinds": kinds_written,
        "simulated": True,
        "correction": False,
        "notice": NOTICE,
        "node_id": payload.get("node_id"),
    }
