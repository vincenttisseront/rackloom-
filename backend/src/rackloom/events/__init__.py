"""Événements de corrélation : source de la chronologie et des flux SSE.

Les données sont filtrées avant insertion ; `dedupe_key` rend chaque émission idempotente.
Après insertion, `pg_notify` réveille les flux SSE (charge utile : identifiant seulement).
"""

from __future__ import annotations

from typing import Any

import psycopg

from ..db import Jsonb
from ..ids import uuid7
from ..redact import Redactor, redact

CHANNEL = "rackloom_events"


def emit(conn: psycopg.Connection, *, type: str, source: str, project_id=None, correlation_id=None,
         environment_id=None, job_id=None, data: dict[str, Any] | None = None, severity: str = "info",
         dedupe_key: str | None = None, redactor: Redactor | None = None) -> int | None:
    payload = (redactor.data(data or {}) if redactor else redact(data or {}))
    key = dedupe_key or f"{type}:{uuid7()}"
    row = conn.execute(
        """
        INSERT INTO events (correlation_id, project_id, environment_id, job_id, source, type, severity,
                            data, dedupe_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING id
        """,
        (correlation_id, project_id, environment_id, job_id, source, type, severity, Jsonb(payload), key),
    ).fetchone()
    if row is None:
        return None
    event_id = row["id"] if isinstance(row, dict) else row[0]
    conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, str(event_id)))
    return event_id
