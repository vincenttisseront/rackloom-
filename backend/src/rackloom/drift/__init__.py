"""F09 : détection de dérive (lecture seule).

Compare le state Terraform attendu (fichiers de labo / instances enregistrées) à l'état
observé. Aucune correction automatique. Une panne d'observation (inventaire vCenter) est
signalée comme `observation_unavailable`, jamais comme une dérive.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from .. import audit, events, inventory
from ..db import Conn, Jsonb
from ..ids import uuid7


def _lab_vm_file(workspaces_parent: Path, deployment_id: UUID, name: str) -> Path | None:
    folder = workspaces_parent / "demo-infra" / str(deployment_id)
    if not folder.is_dir():
        return None
    path = folder / f"{name}.json"
    return path if path.is_file() else None


def scan_deployment(
    conn: Conn,
    *,
    deployment_id: UUID,
    workspaces_parent: Path,
    job_id: UUID | None = None,
    actor_type: str = "system",
    actor_id: str = "drift",
    observation_unavailable: bool = False,
) -> dict[str, Any]:
    """Exécute un scan en lecture seule pour un déploiement."""
    dep = conn.execute("SELECT * FROM deployments WHERE id = %s", (deployment_id,)).fetchone()
    if dep is None:
        raise ValueError("déploiement introuvable")
    expected_by_name = {
        i["name"]: i for i in (dep.get("instances") or []) if isinstance(i, dict) and i.get("name")
    }
    hosts = list(
        conn.execute(
            "SELECT * FROM hosts WHERE deployment_id = %s ORDER BY name", (deployment_id,)
        ).fetchall()
    )
    scan_id = uuid7()
    findings: list[dict[str, Any]] = []
    now = datetime.now(UTC)

    # Observation inventaire : panne vCenter / inventaire → indisponible (pas une dérive).
    snap = inventory.latest_snapshot(conn)
    last_snap = conn.execute(
        "SELECT * FROM inventory_snapshots ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()
    inv_down = observation_unavailable or (
        last_snap is not None and last_snap["status"] == "error" and snap is None
    ) or (last_snap is not None and last_snap["status"] == "error" and snap is not None
          and last_snap["observed_at"] >= snap["observed_at"])
    if inv_down:
        findings.append(
            {
                "resource_name": "*",
                "attribute": "inventory",
                "expected": None,
                "observed": None,
                "source": "vcenter",
                "kind": "observation_unavailable",
                "detected_at": now,
            }
        )
        status = "observation_unavailable"
    else:
        for host in hosts:
            expected = expected_by_name.get(host["name"]) or {}
            path = _lab_vm_file(workspaces_parent, deployment_id, host["name"])
            if path is None:
                continue
            try:
                observed = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                findings.append(
                    {
                        "resource_name": host["name"],
                        "attribute": "lab_file",
                        "expected": "readable_json",
                        "observed": "unreadable",
                        "source": "terraform_state",
                        "kind": "observation_unavailable",
                        "detected_at": now,
                    }
                )
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            for attr in ("vcpu", "memory_mb", "disk_gb"):
                exp = expected.get(attr)
                obs = observed.get(attr)
                if exp is None or obs is None:
                    continue
                if int(exp) != int(obs):
                    findings.append(
                        {
                            "resource_name": host["name"],
                            "attribute": attr,
                            "expected": str(exp),
                            "observed": str(obs),
                            "source": "outside_rackloom",
                            "kind": "drift",
                            "detected_at": mtime,
                        }
                    )
        status = "drift" if any(f["kind"] == "drift" for f in findings) else (
            "observation_unavailable" if findings else "ok"
        )

    conn.execute(
        "INSERT INTO drift_scans (id, project_id, deployment_id, status, source, finished_at, summary, job_id) "
        "VALUES (%s, %s, %s, %s, 'system', now(), %s, %s)",
        (
            scan_id,
            dep["project_id"],
            deployment_id,
            status,
            Jsonb({"finding_count": len(findings), "host_count": len(hosts)}),
            job_id,
        ),
    )
    for f in findings:
        conn.execute(
            "INSERT INTO drift_findings (id, scan_id, project_id, deployment_id, resource_name, attribute, "
            "expected, observed, source, detected_at, kind) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                uuid7(),
                scan_id,
                dep["project_id"],
                deployment_id,
                f["resource_name"],
                f["attribute"],
                f.get("expected"),
                f.get("observed"),
                f["source"],
                f["detected_at"],
                f["kind"],
            ),
        )
    events.emit(
        conn,
        type="drift.scanned",
        source="system",
        project_id=dep["project_id"],
        correlation_id=dep.get("created_by_change_id"),
        data={
            "message": f"Scan de dérive : {status}",
            "deployment_id": str(deployment_id),
            "status": status,
            "findings": len(findings),
        },
    )
    audit.record(
        conn,
        actor_type=actor_type,
        actor_id=actor_id,
        action="drift.scan",
        outcome="ok",
        targets={"deployment_id": str(deployment_id), "status": status},
        versions={"finding_count": str(len(findings))},
    )
    return {
        "scan_id": str(scan_id),
        "status": status,
        "findings": [
            {
                "resource_name": f["resource_name"],
                "attribute": f["attribute"],
                "expected": f.get("expected"),
                "observed": f.get("observed"),
                "source": f["source"],
                "kind": f["kind"],
                "detected_at": f["detected_at"].isoformat(),
            }
            for f in findings
        ],
        "correction": False,
        "notice": "Lecture seule : aucune correction automatique.",
    }


def latest_for_deployment(conn: Conn, deployment_id: UUID) -> dict[str, Any] | None:
    scan = conn.execute(
        "SELECT * FROM drift_scans WHERE deployment_id = %s ORDER BY started_at DESC LIMIT 1",
        (deployment_id,),
    ).fetchone()
    if scan is None:
        return None
    findings = conn.execute(
        "SELECT resource_name, attribute, expected, observed, source, kind, detected_at "
        "FROM drift_findings WHERE scan_id = %s ORDER BY detected_at",
        (scan["id"],),
    ).fetchall()
    return {
        "scan_id": str(scan["id"]),
        "status": scan["status"],
        "started_at": scan["started_at"].isoformat(),
        "finished_at": scan["finished_at"].isoformat() if scan["finished_at"] else None,
        "summary": scan["summary"],
        "correction": False,
        "findings": [
            {
                "resource_name": f["resource_name"],
                "attribute": f["attribute"],
                "expected": f["expected"],
                "observed": f["observed"],
                "source": f["source"],
                "kind": f["kind"],
                "detected_at": f["detected_at"].isoformat(),
            }
            for f in findings
        ],
    }
