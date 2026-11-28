"""Import d'observations Rudder (RAC-36) — lecture seule.

- `rudder-fake` (mode simulated) : fixtures explicites (`rudder_fake`).
- Connecteur `rudder` réel : job `rudder_import` (worker révèle le jeton, HTTPS
  vers endpoint approuvé). Aucune écriture / remédiation Rudder.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import audit, hosts
from ..connectors import rudder as rudder_conn
from ..db import Conn
from ..errors import ApiError, not_found
from ..identity.authz import Principal, require_role
from . import rudder_fake

SOURCE = "rudder"
NOTICE = (
    "Observations importées depuis Rudder (lecture seule). "
    "Labo Rudder réel : non vérifié (D2 / RAC-36). "
    "Remédiation éventuelle derrière l'admission Rackloom uniquement."
)


def write_observations(
    conn: Conn,
    host: dict[str, Any],
    report: dict[str, Any],
    *,
    source: str,
    simulated: bool,
    notice: str | None = None,
    job_id: UUID | None = None,
) -> list[str]:
    """Écrit packages / services / settings depuis un rapport normalisé."""
    kinds_written: list[str] = []
    provenance = {"source": source, "simulated": simulated}
    if notice:
        provenance["notice"] = notice

    if "packages" in report and isinstance(report["packages"], dict):
        hosts.observe(
            conn,
            host,
            "packages",
            {**report["packages"], "_provenance": provenance},
            "ok",
            source,
            job_id,
        )
        kinds_written.append("packages")
    if "services" in report and isinstance(report["services"], dict):
        hosts.observe(conn, host, "services", report["services"], "ok", source, job_id)
        kinds_written.append("services")
    if "settings" in report and isinstance(report["settings"], dict):
        hosts.observe(
            conn,
            host,
            "settings",
            {**report["settings"], "_provenance": provenance},
            "ok",
            source,
            job_id,
        )
        kinds_written.append("settings")
    if not kinds_written:
        raise ApiError(422, "empty_report", "rapport Rudder sans paquets/services/réglages")
    return kinds_written


def _connector_for_import(conn: Conn, connector_id: UUID | None) -> dict[str, Any]:
    if connector_id is not None:
        row = conn.execute("SELECT * FROM connectors WHERE id = %s", (connector_id,)).fetchone()
        if row is None or row["kind"] != "rudder":
            raise not_found("connecteur")
        return row
    rows = conn.execute("SELECT * FROM connectors WHERE kind = 'rudder' ORDER BY slug").fetchall()
    if not rows:
        raise ApiError(409, "rudder_not_configured", "aucun connecteur Rudder configuré")
    if len(rows) == 1:
        return rows[0]
    fake = [r for r in rows if r["mode"] == "simulated"]
    real = [r for r in rows if r["mode"] == "real"]
    if len(fake) == 1 and not real:
        return fake[0]
    if len(real) == 1 and not fake:
        return real[0]
    raise ApiError(
        422,
        "connector_ambiguous",
        "plusieurs connecteurs Rudder : précisez connector_id",
        field="/connector_id",
    )


def request_import(
    conn: Conn,
    principal: Principal,
    host_id: UUID,
    *,
    connector_id: UUID | None = None,
    node_id: str | None = None,
    report: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Importe (fictif synchrone) ou met en file un import réel (worker)."""
    host = conn.execute("SELECT * FROM hosts WHERE id = %s", (host_id,)).fetchone()
    if host is None or not principal.can_see_project(host["project_id"]):
        raise not_found("machine")
    require_role(principal, "operator", host["project_id"], host["environment_id"])

    if report is not None and connector_id is None:
        # Un rapport client ne vise que le connecteur fictif (jamais le réel).
        fake = conn.execute(
            "SELECT * FROM connectors WHERE kind = 'rudder' AND mode = 'simulated' ORDER BY slug LIMIT 1"
        ).fetchone()
        if fake is None:
            raise ApiError(
                422,
                "report_forbidden",
                "rapport client réservé au connecteur Rudder fictif",
                field="/report",
            )
        connector = fake
    else:
        connector = _connector_for_import(conn, connector_id)

    if connector["mode"] == "simulated" or connector["slug"] == "rudder-fake":
        result = rudder_fake.import_observations(conn, principal, host_id, report=report, request_id=request_id)
        return {**result, "connector_id": str(connector["id"]), "connector_slug": connector["slug"]}

    if report is not None:
        raise ApiError(
            422,
            "report_forbidden",
            "un rapport fourni par le client est refusé pour le connecteur Rudder réel",
            field="/report",
        )

    resolved_node = node_id or (connector.get("config") or {}).get("default_node_id")
    if not resolved_node or not isinstance(resolved_node, str):
        raise ApiError(
            422,
            "node_id_required",
            "node_id Rudder requis pour l'import réel",
            field="/node_id",
        )

    url = (connector.get("config") or {}).get("url")
    if not url:
        raise ApiError(409, "not_configured", "URL Rudder non configurée")
    if rudder_conn.TOKEN_SECRET_NAME not in (connector.get("secret_refs") or {}):
        raise ApiError(409, "secret_missing", "jeton Rudder absent (secret « token »)")

    from ..executions import queue

    job = queue.enqueue(
        conn,
        kind="rudder_import",
        project_id=host["project_id"],
        payload={
            "host_id": str(host_id),
            "connector_id": str(connector["id"]),
            "node_id": resolved_node,
            "requested_by": str(principal.user_id),
        },
        idempotency_key=f"rudder_import:{host_id}:{connector['id']}:{resolved_node}",
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="rudder.import_queued",
        outcome="ok",
        project_id=host["project_id"],
        targets={
            "host_id": str(host_id),
            "connector": connector["slug"],
            "job_id": str(job["id"]),
            "node_id": resolved_node,
        },
        request_id=request_id,
    )
    return {
        "host_id": str(host_id),
        "source": SOURCE,
        "simulated": False,
        "queued": True,
        "job_id": str(job["id"]),
        "connector_id": str(connector["id"]),
        "connector_slug": connector["slug"],
        "node_id": resolved_node,
        "correction": False,
        "notice": NOTICE,
    }


def apply_fetched_report(
    conn: Conn,
    *,
    host_id: UUID,
    report: dict[str, Any],
    job_id: UUID | None,
    actor_id: str,
) -> dict[str, Any]:
    """Persiste un rapport déjà récupéré (appel worker)."""
    host = conn.execute("SELECT * FROM hosts WHERE id = %s", (host_id,)).fetchone()
    if host is None:
        raise not_found("machine")
    kinds = write_observations(
        conn,
        host,
        report,
        source=SOURCE,
        simulated=False,
        notice=NOTICE,
        job_id=job_id,
    )
    audit.record(
        conn,
        actor_type="worker",
        actor_id=actor_id,
        action="rudder.import_observations",
        outcome="ok",
        project_id=host["project_id"],
        targets={"host_id": str(host_id), "kinds": ",".join(kinds)},
        versions={"simulated": "false", "node_id": str(report.get("node_id") or "")},
    )
    return {
        "host_id": str(host_id),
        "source": SOURCE,
        "kinds": kinds,
        "simulated": False,
        "correction": False,
        "notice": NOTICE,
        "node_id": report.get("node_id"),
    }
