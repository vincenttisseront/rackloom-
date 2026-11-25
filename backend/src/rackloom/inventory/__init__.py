"""Inventaire : instantanés horodatés et lecture filtrée par octrois et fraîcheur.

Au lot 1, la source est le connecteur `vsphere-fake` (SIMULÉ) alimenté par
`lab/fixtures/inventory.yaml` et par l'observation des fichiers créés par Terraform.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import yaml

from ..db import Jsonb
from ..ids import uuid7


def load_fixture(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def record_snapshot(conn: psycopg.Connection, connector_id: UUID, items: list[dict[str, Any]],
                    observed_at: datetime | None = None) -> UUID:
    observed = observed_at or datetime.now(UTC)
    snapshot_id = uuid7()
    conn.execute(
        "INSERT INTO inventory_snapshots (id, connector_id, observed_at, status, item_count) "
        "VALUES (%s, %s, %s, 'ok', %s)",
        (snapshot_id, connector_id, observed, len(items)),
    )
    for item in items:
        conn.execute(
            """
            INSERT INTO inventory_items (id, snapshot_id, connector_id, kind, stable_id, name, parent_stable_id,
                                         attributes, observed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (uuid7(), snapshot_id, connector_id, item["kind"], item["stable_id"], item["name"],
             item.get("parent"), Jsonb(item.get("attributes", {})), observed),
        )
    return snapshot_id


def record_failure(conn: psycopg.Connection, connector_id: UUID, error: str) -> None:
    conn.execute(
        "INSERT INTO inventory_snapshots (id, connector_id, observed_at, status, error) "
        "VALUES (%s, %s, now(), 'error', %s)",
        (uuid7(), connector_id, error[:500]),
    )


def latest_snapshot(conn: psycopg.Connection, connector_id: UUID | None = None) -> dict[str, Any] | None:
    if connector_id is None:
        return conn.execute(
            "SELECT * FROM inventory_snapshots WHERE status = 'ok' ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()
    return conn.execute(
        "SELECT * FROM inventory_snapshots WHERE status = 'ok' AND connector_id = %s "
        "ORDER BY observed_at DESC LIMIT 1",
        (connector_id,),
    ).fetchone()


def granted_items(conn: psycopg.Connection, project_id: UUID, environment_id: UUID | None = None,
                  kind: str | None = None) -> list[dict[str, Any]]:
    """Éléments du dernier instantané octroyés au projet (et à l'environnement si fourni)."""
    snap = latest_snapshot(conn)
    if snap is None:
        return []
    query = """
        SELECT DISTINCT ON (i.kind, i.stable_id) i.kind, i.stable_id, i.name, i.parent_stable_id, i.attributes,
               i.observed_at, g.environment_id, c.slug AS connector, c.mode AS connector_mode
        FROM inventory_items i
        JOIN resource_grants g ON g.kind = i.kind AND g.stable_id = i.stable_id AND g.connector_id = i.connector_id
        JOIN connectors c ON c.id = i.connector_id
        WHERE i.snapshot_id = %s AND g.project_id = %s
    """
    params: list[Any] = [snap["id"], project_id]
    if environment_id is not None:
        query += " AND g.environment_id = %s"
        params.append(environment_id)
    if kind is not None:
        query += " AND i.kind = %s"
        params.append(kind)
    query += " ORDER BY i.kind, i.stable_id"
    return conn.execute(query, params).fetchall()


def freshness(conn: psycopg.Connection, max_age_hours: int) -> dict[str, Any]:
    snap = latest_snapshot(conn)
    if snap is None:
        return {"snapshot_id": None, "observed_at": None, "age_seconds": None, "stale": True}
    age = (datetime.now(UTC) - snap["observed_at"]).total_seconds()
    return {"snapshot_id": str(snap["id"]), "observed_at": snap["observed_at"].isoformat(),
            "age_seconds": int(age), "stale": age > max_age_hours * 3600}
