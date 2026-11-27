"""Fiches machine : enrôlement, ancres de clés d'hôte, observations et état évalué.

Ordre d'évaluation de l'état : aucune observation → non collecté ; dernière collecte échouée →
inaccessible ; dernière observation plus vieille que le seuil → données anciennes ; service
suivi inactif ou sonde en échec → en erreur ; sinon sain.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from ..db import Conn, Jsonb
from ..ids import uuid7
from ..redact import Redactor

STALE_AFTER = timedelta(minutes=15)

HEALTH_LABELS = {
    "not_collected": "Non collecté",
    "unreachable": "Inaccessible",
    "stale": "Données anciennes",
    "error": "En erreur",
    "healthy": "Sain",
}


def fingerprint(public_key_line: str) -> str:
    """Empreinte SHA256 au format OpenSSH."""
    parts = public_key_line.split()
    raw = base64.b64decode(parts[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def import_anchor(
    conn: Conn, address: str, public_key_line: str, source: str, imported_by: UUID | None = None
) -> dict[str, Any]:
    parts = public_key_line.strip().split()
    if len(parts) < 2 or parts[0] not in ("ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"):
        raise ValueError("clé publique d'hôte invalide")
    fp = fingerprint(public_key_line)
    return conn.execute(
        """
        INSERT INTO trust_anchors (id, address, key_type, public_key, fingerprint, source, imported_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (address, key_type) DO UPDATE SET public_key = EXCLUDED.public_key,
            fingerprint = EXCLUDED.fingerprint, source = EXCLUDED.source, imported_at = now()
        RETURNING *
        """,
        (uuid7(), address, parts[0], f"{parts[0]} {parts[1]}", fp, source, imported_by),
    ).fetchone()


def known_hosts(conn: Conn, addresses: list[str]) -> str:
    rows = conn.execute(
        "SELECT address, public_key FROM trust_anchors WHERE address = ANY(%s) ORDER BY address", (addresses,)
    ).fetchall()
    return "".join(f"{r['address']} {r['public_key']}\n" for r in rows)


def anchor_for(conn: Conn, address: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM trust_anchors WHERE address = %s ORDER BY key_type LIMIT 1", (address,)
    ).fetchone()


def observe(
    conn: Conn,
    host: dict[str, Any],
    kind: str,
    data: dict[str, Any],
    status: str,
    source: str,
    job_id: UUID | None,
    redactor: Redactor | None = None,
) -> None:
    redactor = redactor or Redactor()
    conn.execute(
        "INSERT INTO host_observations (host_id, project_id, kind, data, status, source, job_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (host["id"], host["project_id"], kind, Jsonb(redactor.data(data)), status, source, job_id),
    )


def latest_observations(conn: Conn, host_id: UUID) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT DISTINCT ON (kind) * FROM host_observations WHERE host_id = %s ORDER BY kind, collected_at DESC",
        (host_id,),
    ).fetchall()
    return {r["kind"]: r for r in rows}


def health(conn: Conn, host: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    obs = latest_observations(conn, host["id"])
    if not obs:
        return {"state": "not_collected", "label": HEALTH_LABELS["not_collected"], "last_collected_at": None}
    last = max(obs.values(), key=lambda o: o["collected_at"])
    result: dict[str, Any] = {"last_collected_at": last["collected_at"].isoformat()}
    if last["status"] in ("unreachable", "error") and last["kind"] in ("ssh", "facts"):
        state = "unreachable"
    elif now - last["collected_at"] > STALE_AFTER:
        state = "stale"
    else:
        services = obs.get("services")
        probe = obs.get("probe")
        failing = any(o is not None and o["status"] != "ok" for o in (services, probe))
        state = "error" if failing else "healthy"
    result.update(state=state, label=HEALTH_LABELS[state])
    return result


def view(conn: Conn, host: dict[str, Any]) -> dict[str, Any]:
    obs = latest_observations(conn, host["id"])
    stack = conn.execute("SELECT slug FROM stacks WHERE id = %s", (host["stack_id"],)).fetchone()
    change = conn.execute("SELECT seq, state FROM changes WHERE id = %s", (host["change_id"],)).fetchone()
    return {
        "id": str(host["id"]),
        "name": host["name"],
        "project_id": str(host["project_id"]),
        "environment_id": str(host["environment_id"]),
        "vcenter": {"vm_stable_id": host["vm_stable_id"], "simulated": True},
        "stack": stack["slug"] if stack else None,
        "address": host["address"],
        "address_simulated": True,
        "host_key_fingerprint": host["host_key_fingerprint"],
        "enrolled_at": _iso(host["enrolled_at"]),
        "deployment_state": host["deployment_state"],
        "composition_member": host.get("composition_member"),
        "change": {"id": str(host["change_id"]), "ref": f"CHG-{change['seq']:04d}", "state": change["state"]},
        "health": health(conn, host),
        "facts": obs["facts"]["data"] if "facts" in obs else None,
        "packages": obs["packages"]["data"] if "packages" in obs else None,
        "services": obs["services"]["data"] if "services" in obs else None,
        "probe": obs["probe"]["data"] if "probe" in obs else None,
        "provenance": {
            "source": "rackloom-collect (Ansible, sans élévation)",
            "simulated_parts": ["identité vCenter", "adresse IP (nœud de labo associé)"],
        },
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
