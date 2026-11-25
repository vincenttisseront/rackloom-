"""Journal d'audit en ajout seul, chaîné par hachage.

Chaque ligne porte `prev_hash` et `hash = SHA-256(prev_hash || JSON canonique de la ligne)`.
`verify()` recalcule la chaîne et signale la première ligne altérée.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import psycopg

from ..canonical import canonical_json, sha256_hex
from ..db import Conn, Jsonb
from ..redact import redact

GENESIS = "0" * 64
AUDIT_LOCK = 4242002


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "at": row["at"].astimezone(UTC).isoformat(timespec="microseconds"),
        "actor_type": row["actor_type"],
        "actor_id": row["actor_id"],
        "action": row["action"],
        "project_id": str(row["project_id"]) if row["project_id"] else None,
        "correlation_id": str(row["correlation_id"]) if row["correlation_id"] else None,
        "targets": row["targets"],
        "versions": row["versions"],
        "policy_decision": row["policy_decision"],
        "outcome": row["outcome"],
        "request_id": row["request_id"],
    }


def _normalize(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def record(conn: Conn, *, actor_type: str, actor_id: str | None, action: str,
           outcome: str, project_id=None, correlation_id=None, targets: dict | None = None,
           versions: dict | None = None, policy_decision: dict | None = None,
           request_id: str | None = None) -> int:
    """À appeler dans la transaction qui porte l'action auditée."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK,))
    last = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = (last["hash"] if isinstance(last, dict) else last[0]) if last else GENESIS
    row = {
        "at": datetime.now(UTC),
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action,
        "project_id": project_id,
        "correlation_id": correlation_id,
        "targets": _normalize(redact(targets or {})),
        "versions": _normalize(versions or {}),
        "policy_decision": _normalize(policy_decision) if policy_decision is not None else None,
        "outcome": outcome,
        "request_id": request_id,
    }
    digest = sha256_hex(prev_hash.encode() + canonical_json(_payload(row)))
    cur = conn.execute(
        """
        INSERT INTO audit_log (at, actor_type, actor_id, action, project_id, correlation_id, targets,
                               versions, policy_decision, outcome, request_id, prev_hash, hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (row["at"], actor_type, actor_id, action, project_id, correlation_id, Jsonb(row["targets"]),
         Jsonb(row["versions"]), Jsonb(row["policy_decision"]) if policy_decision is not None else None,
         outcome, request_id, prev_hash, digest),
    )
    result = cur.fetchone()
    return result["id"] if isinstance(result, dict) else result[0]


def verify(conn: Conn) -> dict[str, Any]:
    prev = GENESIS
    count = 0
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT * FROM audit_log ORDER BY id")
        for row in cur:
            expected = sha256_hex(prev.encode() + canonical_json(_payload(row)))
            if row["prev_hash"] != prev or row["hash"] != expected:
                return {"ok": False, "checked": count, "first_bad_id": row["id"]}
            prev = row["hash"]
            count += 1
    return {"ok": True, "checked": count, "first_bad_id": None}
