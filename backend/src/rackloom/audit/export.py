"""F44 : copie d'audit hors appliance.

Destination locale à verrouillage d'objets (simulé / testable sans stockage d'entreprise).
Reprise sans doublons via `dedupe_key` (= hash de la ligne d'audit). Les secrets sont
retirés avant écriture. Un objet verrouillé refuse l'écrasement, y compris par un admin.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from ..canonical import canonical_json
from ..config import get_settings
from ..db import Conn
from ..errors import ApiError, forbidden, not_found
from ..identity.authz import Principal
from ..ids import uuid7
from ..redact import redact
from . import record as audit_record


def _root(relative: str) -> Path:
    base = get_settings().data_dir / "audit-export"
    path = (base / relative).resolve()
    if not str(path).startswith(str(base.resolve())):
        raise ApiError(422, "invalid_path", "chemin de destination hors périmètre")
    return path


def create_destination(
    conn: Conn,
    principal: Principal,
    *,
    slug: str,
    relative_path: str,
    object_lock: bool = True,
) -> dict[str, Any]:
    if not principal.is_platform_admin:
        raise forbidden("rôle administrateur de plateforme requis")
    text = (slug or "").strip()
    if not text or len(text) > 40:
        raise ApiError(422, "invalid_slug", "slug invalide", field="/slug")
    rel = (relative_path or text).strip().lstrip("/")
    if not rel or ".." in rel or len(rel) > 200:
        raise ApiError(422, "invalid_path", "chemin relatif invalide", field="/relative_path")
    dest_id = uuid7()
    conn.execute(
        """
        INSERT INTO audit_export_destinations
            (id, slug, kind, relative_path, object_lock, simulated, created_by)
        VALUES (%s, %s, 'local_object_lock', %s, %s, true, %s)
        """,
        (dest_id, text, rel, object_lock, principal.user_id),
    )
    conn.execute(
        "INSERT INTO audit_export_cursors (destination_id, last_audit_id) VALUES (%s, 0)",
        (dest_id,),
    )
    root = _root(rel)
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / ".object-lock").write_text("locked\n" if object_lock else "unlocked\n", encoding="utf-8")
    except OSError as exc:
        raise ApiError(
            503,
            "storage_unavailable",
            "stockage d'export d'audit indisponible (volume non monté ou non inscriptible)",
        ) from exc
    audit_record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="audit.export.destination.create",
        outcome="ok",
        targets={"destination_id": str(dest_id), "slug": text, "object_lock": object_lock},
    )
    return get_destination(conn, principal, dest_id)


def get_destination(conn: Conn, principal: Principal, destination_id: UUID) -> dict[str, Any]:
    if not (principal.is_platform_admin or principal.is_global_auditor):
        raise forbidden("rôle administrateur ou auditeur requis")
    row = conn.execute("SELECT * FROM audit_export_destinations WHERE id = %s", (destination_id,)).fetchone()
    if row is None:
        raise not_found("destination d'export")
    cursor = conn.execute(
        "SELECT last_audit_id FROM audit_export_cursors WHERE destination_id = %s", (destination_id,)
    ).fetchone()
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "kind": row["kind"],
        "relative_path": row["relative_path"],
        "object_lock": row["object_lock"],
        "simulated": row["simulated"],
        "last_audit_id": cursor["last_audit_id"] if cursor else 0,
    }


def list_destinations(conn: Conn, principal: Principal) -> list[dict[str, Any]]:
    if not (principal.is_platform_admin or principal.is_global_auditor):
        raise forbidden("rôle administrateur ou auditeur requis")
    rows = conn.execute("SELECT id FROM audit_export_destinations ORDER BY slug").fetchall()
    return [get_destination(conn, principal, r["id"]) for r in rows]


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    return redact(
        {
            "id": row["id"],
            "at": row["at"].isoformat(),
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
            "prev_hash": row["prev_hash"],
            "hash": row["hash"],
        }
    )


def _write_locked(path: Path, body: bytes, *, object_lock: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_marker = path.with_suffix(path.suffix + ".lock")
    if path.exists() and object_lock:
        raise ApiError(
            409,
            "object_locked",
            "copie d'audit verrouillée : écrasement refusé (rétention externe)",
        )
    if path.exists() and not object_lock:
        # Sans verrou : encore interdit de modifier une livraison déjà déposée via l'API admin.
        raise ApiError(409, "object_exists", "objet déjà déposé")
    path.write_bytes(body)
    if object_lock:
        lock_marker.write_text("WORM\n", encoding="utf-8")
        try:
            path.chmod(0o444)
            lock_marker.chmod(0o444)
        except OSError:
            pass


def enqueue_pending(conn: Conn, destination_id: UUID, *, limit: int = 500) -> int:
    dest = conn.execute("SELECT * FROM audit_export_destinations WHERE id = %s", (destination_id,)).fetchone()
    if dest is None:
        raise not_found("destination d'export")
    cursor = conn.execute(
        "SELECT last_audit_id FROM audit_export_cursors WHERE destination_id = %s FOR UPDATE",
        (destination_id,),
    ).fetchone()
    last = cursor["last_audit_id"] if cursor else 0
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE id > %s ORDER BY id LIMIT %s",
        (last, limit),
    ).fetchall()
    n = 0
    max_id = last
    for row in rows:
        dedupe = row["hash"]
        object_key = f"{row['at'].strftime('%Y/%m/%d')}/{row['id']}-{row['hash'][:16]}.json"
        inserted = conn.execute(
            """
            INSERT INTO audit_export_deliveries
                (id, destination_id, audit_id, dedupe_key, object_key, status)
            VALUES (%s, %s, %s, %s, %s, 'pending')
            ON CONFLICT (destination_id, dedupe_key) DO NOTHING
            RETURNING id
            """,
            (uuid7(), destination_id, row["id"], dedupe, object_key),
        ).fetchone()
        if inserted:
            n += 1
        max_id = max(max_id, row["id"])
    if max_id > last:
        conn.execute(
            "UPDATE audit_export_cursors SET last_audit_id = %s, updated_at = now() WHERE destination_id = %s",
            (max_id, destination_id),
        )
    return n


def deliver_pending(conn: Conn, destination_id: UUID, *, limit: int = 100) -> dict[str, Any]:
    dest = conn.execute("SELECT * FROM audit_export_destinations WHERE id = %s", (destination_id,)).fetchone()
    if dest is None:
        raise not_found("destination d'export")
    root = _root(dest["relative_path"])
    pending = conn.execute(
        """
        SELECT d.*, a.hash AS audit_hash, a.at, a.actor_type, a.actor_id, a.action, a.project_id,
               a.correlation_id, a.targets, a.versions, a.policy_decision, a.outcome, a.request_id,
               a.prev_hash, a.hash, a.id AS audit_row_id
        FROM audit_export_deliveries d
        JOIN audit_log a ON a.id = d.audit_id
        WHERE d.destination_id = %s AND d.status IN ('pending', 'failed')
        ORDER BY d.audit_id
        LIMIT %s
        """,
        (destination_id, limit),
    ).fetchall()
    delivered = failed = skipped = 0
    for row in pending:
        payload = _payload(
            {
                "id": row["audit_row_id"],
                "at": row["at"],
                "actor_type": row["actor_type"],
                "actor_id": row["actor_id"],
                "action": row["action"],
                "project_id": row["project_id"],
                "correlation_id": row["correlation_id"],
                "targets": row["targets"],
                "versions": row["versions"],
                "policy_decision": row["policy_decision"],
                "outcome": row["outcome"],
                "request_id": row["request_id"],
                "prev_hash": row["prev_hash"],
                "hash": row["hash"],
            }
        )
        body = canonical_json(payload)
        target = root / row["object_key"]
        try:
            if target.exists() and dest["object_lock"]:
                # Déjà déposé : marquer livré sans réécrire (reprise sans doublon).
                conn.execute(
                    "UPDATE audit_export_deliveries SET status = 'delivered', "
                    "delivered_at = COALESCE(delivered_at, now()), "
                    "attempts = attempts + 1, last_error = NULL WHERE id = %s",
                    (row["id"],),
                )
                skipped += 1
                continue
            _write_locked(target, body, object_lock=bool(dest["object_lock"]))
            conn.execute(
                "UPDATE audit_export_deliveries SET status = 'delivered', delivered_at = now(), "
                "attempts = attempts + 1, last_error = NULL WHERE id = %s",
                (row["id"],),
            )
            delivered += 1
        except ApiError as exc:
            if exc.code == "object_locked":
                conn.execute(
                    "UPDATE audit_export_deliveries SET status = 'delivered', "
                    "delivered_at = COALESCE(delivered_at, now()), attempts = attempts + 1, "
                    "last_error = NULL WHERE id = %s",
                    (row["id"],),
                )
                skipped += 1
            else:
                conn.execute(
                    "UPDATE audit_export_deliveries SET status = 'failed', attempts = attempts + 1, "
                    "last_error = %s WHERE id = %s",
                    (exc.message[:500], row["id"]),
                )
                failed += 1
        except OSError as exc:
            conn.execute(
                "UPDATE audit_export_deliveries SET status = 'failed', attempts = attempts + 1, "
                "last_error = %s WHERE id = %s",
                (str(exc)[:500], row["id"]),
            )
            failed += 1
    return {
        "destination_id": str(destination_id),
        "delivered": delivered,
        "failed": failed,
        "already_present": skipped,
        "simulated": True,
    }


def run_export(conn: Conn, principal: Principal, destination_id: UUID) -> dict[str, Any]:
    if not principal.is_platform_admin:
        raise forbidden("rôle administrateur de plateforme requis")
    queued = enqueue_pending(conn, destination_id)
    result = deliver_pending(conn, destination_id)
    audit_record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="audit.export.run",
        outcome="ok",
        targets={"destination_id": str(destination_id), "queued": queued, **result},
    )
    return {"queued": queued, **result}


def try_overwrite(conn: Conn, principal: Principal, destination_id: UUID, audit_id: int) -> dict[str, Any]:
    """Tentative d'écrasement admin : doit échouer si object_lock."""
    if not principal.is_platform_admin:
        raise forbidden("rôle administrateur de plateforme requis")
    dest = conn.execute("SELECT * FROM audit_export_destinations WHERE id = %s", (destination_id,)).fetchone()
    if dest is None:
        raise not_found("destination d'export")
    delivery = conn.execute(
        "SELECT * FROM audit_export_deliveries WHERE destination_id = %s AND audit_id = %s",
        (destination_id, audit_id),
    ).fetchone()
    if delivery is None:
        raise not_found("livraison")
    path = _root(dest["relative_path"]) / delivery["object_key"]
    try:
        _write_locked(path, b'{"tampered":true}', object_lock=bool(dest["object_lock"]))
    except ApiError as exc:
        audit_record(
            conn,
            actor_type="user",
            actor_id=str(principal.user_id),
            action="audit.export.overwrite",
            outcome="denied",
            targets={"destination_id": str(destination_id), "audit_id": audit_id, "code": exc.code},
        )
        raise
    return {"overwritten": True}


def read_delivered(conn: Conn, destination_id: UUID, audit_id: int) -> dict[str, Any]:
    dest = conn.execute("SELECT * FROM audit_export_destinations WHERE id = %s", (destination_id,)).fetchone()
    if dest is None:
        raise not_found("destination d'export")
    delivery = conn.execute(
        "SELECT * FROM audit_export_deliveries WHERE destination_id = %s AND audit_id = %s AND status = 'delivered'",
        (destination_id, audit_id),
    ).fetchone()
    if delivery is None:
        raise not_found("livraison")
    path = _root(dest["relative_path"]) / delivery["object_key"]
    if not path.is_file():
        raise not_found("objet")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"object_key": delivery["object_key"], "payload": data, "locked": dest["object_lock"]}
