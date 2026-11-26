"""Événements de corrélation : source de la chronologie et des flux SSE.

Les données sont filtrées avant insertion ; `dedupe_key` rend chaque émission idempotente.
Après insertion, `pg_notify` réveille les flux SSE (charge utile : identifiant seulement).

Sources (ADR-0006) : request, approval, admission, job, terraform, ansible, vcenter,
verification, chat, system. Les sorties brutes d'outils n'entrent jamais dans `data`.
"""

from __future__ import annotations

from typing import Any

from ..db import Conn, Jsonb
from ..ids import uuid7
from ..redact import Redactor, redact

CHANNEL = "rackloom_events"

SOURCES = frozenset(
    {
        "request",
        "approval",
        "admission",
        "job",
        "terraform",
        "ansible",
        "vcenter",
        "verification",
        "chat",
        "system",
    }
)

# Alias historiques → vocabulaire ADR (émis avant F21).
SOURCE_ALIASES = {
    "rackloom": "system",
    "worker": "job",
    "reaper": "system",
    "assistant": "chat",
    "vsphere-fake": "vcenter",
    "nginx": "system",
}

# Jamais dans un événement : sorties d'outils, plans bruts, journaux (TB6 / F21).
RAW_KEYS = frozenset(
    {
        "stdout",
        "stderr",
        "stdin",
        "plan",
        "plan_json",
        "plan_bin",
        "raw",
        "log",
        "logs",
        "output",
        "outputs",
        "report",
        "reports",
        "hcl",
        "tfstate",
        "binary",
        "workspace",
        "artifact_bytes",
        "plaintext",
        "body",
        "payload_raw",
    }
)


def normalize_source(source: str) -> str:
    value = SOURCE_ALIASES.get(source, source)
    if value not in SOURCES:
        raise ValueError(f"source d'événement inconnue : {source}")
    return value


def scrub_event_data(data: dict[str, Any]) -> dict[str, Any]:
    """Retire les clés de sorties brutes (récursif) avant stockage."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        low = key.lower()
        if key in RAW_KEYS or low in RAW_KEYS or low.endswith("_log") or low.endswith("_stdout"):
            continue
        if isinstance(value, dict):
            out[key] = scrub_event_data(value)
        elif isinstance(value, list):
            out[key] = [scrub_event_data(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


def emit(
    conn: Conn,
    *,
    type: str,
    source: str,
    project_id=None,
    correlation_id=None,
    environment_id=None,
    job_id=None,
    data: dict[str, Any] | None = None,
    severity: str = "info",
    dedupe_key: str | None = None,
    redactor: Redactor | None = None,
) -> int | None:
    src = normalize_source(source)
    payload = scrub_event_data(redactor.data(data or {}) if redactor else redact(data or {}))
    key = dedupe_key or f"{type}:{uuid7()}"
    row = conn.execute(
        """
        INSERT INTO events (correlation_id, project_id, environment_id, job_id, source, type, severity,
                            data, dedupe_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING id
        """,
        (correlation_id, project_id, environment_id, job_id, src, type, severity, Jsonb(payload), key),
    ).fetchone()
    if row is None:
        return None
    event_id = row["id"] if isinstance(row, dict) else row[0]
    conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, str(event_id)))
    return event_id
