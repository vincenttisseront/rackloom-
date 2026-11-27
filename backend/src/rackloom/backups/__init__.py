"""F39 : vérification des sauvegardes des VM.

Source fictive locale (`backup_proofs`). Un snapshot vSphere n'est jamais une preuve.
Pour un changement sensible (politique de sauvegarde ≠ none), R115 exige une preuve
approuvée et fraîche. Observations fictives explicites (`simulated=true`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .. import policy
from ..db import Conn
from ..ids import uuid7

PROOF_APPROVED = "approved_backup"
PROOF_SNAPSHOT = "vsphere_snapshot"


def requirements_from(resolution: dict[str, Any] | None) -> dict[str, Any] | None:
    """Exigence de sauvegarde figée dans la révision ; None = R115 sans effet."""
    if not resolution:
        return None
    backup = resolution.get("backup")
    if not backup or backup.get("policy") in (None, "none", False):
        return None
    max_age = int(backup.get("max_age_hours") or 24)
    identities = [i.get("name") or i.get("vm_uuid") for i in (resolution.get("instances") or []) if isinstance(i, dict)]
    identities = [str(x) for x in identities if x]
    # Restauration : identité source explicite.
    source = (resolution.get("restore") or {}).get("source_instance")
    if source:
        identities = [str(source)]
    if not identities:
        return None
    return {
        "policy": backup.get("policy"),
        "max_age_hours": max_age,
        "identities": identities,
        "sensitive": True,
    }


def set_proof(
    conn: Conn,
    vm_identity: str,
    *,
    status: str = "ok",
    proof_kind: str = PROOF_APPROVED,
    last_backup_at: datetime | None = None,
    reachable: bool = True,
    note: str = "",
) -> dict[str, Any]:
    """Enregistre une observation fictive de sauvegarde (tests / démo)."""
    if proof_kind not in (PROOF_APPROVED, PROOF_SNAPSHOT):
        raise ValueError(proof_kind)
    if status not in ("ok", "stale", "missing", "unreachable"):
        raise ValueError(status)
    when = last_backup_at or datetime.now(UTC)
    row = conn.execute("SELECT id FROM backup_proofs WHERE vm_identity = %s", (vm_identity,)).fetchone()
    if row:
        return conn.execute(
            "UPDATE backup_proofs SET status = %s, proof_kind = %s, last_backup_at = %s, "
            "reachable = %s, note = %s, simulated = true, updated_at = now() WHERE id = %s RETURNING *",
            (status, proof_kind, when, reachable, note, row["id"]),
        ).fetchone()
    return conn.execute(
        "INSERT INTO backup_proofs "
        "(id, vm_identity, status, proof_kind, last_backup_at, reachable, simulated, note) "
        "VALUES (%s, %s, %s, %s, %s, %s, true, %s) RETURNING *",
        (uuid7(), vm_identity, status, proof_kind, when, reachable, note),
    ).fetchone()


def clear_proof(conn: Conn, vm_identity: str) -> None:
    conn.execute("DELETE FROM backup_proofs WHERE vm_identity = %s", (vm_identity,))


def _fresh(row: dict[str, Any], max_age_hours: int, now: datetime) -> bool:
    if row["last_backup_at"] is None:
        return False
    age = now - row["last_backup_at"]
    return age <= timedelta(hours=max_age_hours)


def check(conn: Conn, requirements: dict[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any]:
    """status : ok | stale | missing | unverified | snapshot_rejected."""
    if not requirements:
        return {"status": "ok", "items": [], "simulated": True}
    now = now or datetime.now(UTC)
    max_age = int(requirements["max_age_hours"])
    items: list[dict[str, Any]] = []
    status = "ok"
    for identity in requirements["identities"]:
        row = conn.execute("SELECT * FROM backup_proofs WHERE vm_identity = %s", (identity,)).fetchone()
        if row is None:
            items.append(
                {
                    "vm_identity": identity,
                    "status": "missing",
                    "proof_kind": None,
                    "simulated": True,
                    "note": "aucune preuve de sauvegarde auprès de la source approuvée",
                }
            )
            if status not in ("snapshot_rejected", "unverified"):
                status = "missing"
            continue
        if not row["reachable"] or row["status"] == "unreachable":
            items.append(
                {
                    "vm_identity": identity,
                    "status": "unverified",
                    "proof_kind": row["proof_kind"],
                    "simulated": True,
                    "note": "source de sauvegarde injoignable",
                }
            )
            status = "unverified"
            continue
        if row["proof_kind"] == PROOF_SNAPSHOT:
            items.append(
                {
                    "vm_identity": identity,
                    "status": "snapshot_rejected",
                    "proof_kind": PROOF_SNAPSHOT,
                    "simulated": True,
                    "note": "un snapshot vSphere ne vaut pas preuve de sauvegarde",
                }
            )
            status = "snapshot_rejected"
            continue
        if row["status"] == "missing":
            items.append(
                {
                    "vm_identity": identity,
                    "status": "missing",
                    "proof_kind": row["proof_kind"],
                    "simulated": True,
                    "note": row["note"] or "sauvegarde introuvable",
                }
            )
            if status == "ok":
                status = "missing"
            continue
        if row["status"] == "stale" or not _fresh(row, max_age, now):
            items.append(
                {
                    "vm_identity": identity,
                    "status": "stale",
                    "proof_kind": row["proof_kind"],
                    "last_backup_at": row["last_backup_at"].isoformat() if row["last_backup_at"] else None,
                    "simulated": True,
                    "note": f"sauvegarde périmée (max {max_age} h)",
                }
            )
            if status == "ok":
                status = "stale"
            continue
        items.append(
            {
                "vm_identity": identity,
                "status": "ok",
                "proof_kind": row["proof_kind"],
                "last_backup_at": row["last_backup_at"].isoformat() if row["last_backup_at"] else None,
                "simulated": True,
                "note": "preuve approuvée (fictive)",
            }
        )
    return {"status": status, "items": items, "simulated": True, "policy": requirements.get("policy")}


def unverified_policy() -> str:
    return str(policy.BUILTIN_RULES["platform"].get("backup_unverified_policy", "block"))


def stale_policy() -> str:
    return str(policy.BUILTIN_RULES["platform"].get("backup_stale_policy", "block"))


def refusal(result: dict[str, Any]) -> tuple[str, str]:
    status = result["status"]
    if status == "snapshot_rejected":
        return "backup_snapshot_not_proof", (
            "un snapshot vSphere ne vaut pas preuve de sauvegarde. "
            "Fournissez une preuve auprès de la source de sauvegarde approuvée."
        )
    if status == "missing":
        return "backup_missing", "sauvegarde introuvable auprès de la source approuvée."
    if status == "stale":
        return "backup_stale", "sauvegarde périmée selon la politique du profil."
    return "backup_unverified", (
        "contrôle de sauvegarde non vérifiable (source injoignable). "
        "La politique bloque le changement sensible jusqu'à vérification."
    )
