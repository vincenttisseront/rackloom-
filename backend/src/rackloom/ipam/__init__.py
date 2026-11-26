"""F03 : réservations IPAM/DNS — préparation, réservation, confirmation, libération.

Une prévisualisation (`preview=True`) n'écrit aucune réservation. Deux demandes concurrentes
ne peuvent pas obtenir la même adresse (contrainte UNIQUE + verrou de pool). Un résultat
d'exécution `uncertain` place la réservation en `held_uncertain` : elle n'est pas libérée
avant réconciliation.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import audit
from ..db import Conn
from ..errors import ApiError
from ..ids import uuid7
from . import fake as ipam_fake

ACTIVE = ("prepared", "reserved", "confirmed", "held_uncertain")


def default_pool(conn: Conn, *, lock: bool = False) -> dict[str, Any]:
    query = "SELECT * FROM ipam_pools WHERE slug = 'lab-demo'" + (" FOR UPDATE" if lock else "")
    row = conn.execute(query).fetchone()
    if row is None:
        raise ApiError(503, "ipam_unavailable", "aucune plage IPAM de laboratoire configurée")
    return row


def _used_addresses(conn: Conn, pool_id: UUID) -> set[str]:
    rows = conn.execute(
        "SELECT host(address)::text AS address FROM ipam_reservations "
        "WHERE pool_id = %s AND state = ANY(%s)",
        (pool_id, list(ACTIVE)),
    ).fetchall()
    return {r["address"] for r in rows}


def preview(conn: Conn, *, hostname: str) -> dict[str, Any]:
    """Propose une adresse et un nom DNS sans créer de réservation externe ni locale."""
    rows = preview_many(conn, hostnames=[hostname])
    return rows[0]


def preview_many(conn: Conn, *, hostnames: list[str]) -> list[dict[str, Any]]:
    """Prévisualise N adresses distinctes sans aucune écriture."""
    pool = default_pool(conn)
    used = _used_addresses(conn, pool["id"])
    out: list[dict[str, Any]] = []
    for hostname in hostnames:
        address = ipam_fake.next_address(used, pool["cidr"])
        if address is None:
            raise ApiError(409, "ipam_exhausted", "aucune adresse libre dans la plage de laboratoire")
        used.add(address)
        dns = ipam_fake.dns_name_for(hostname, pool["dns_suffix"])
        out.append(
            {
                "preview": True,
                "reservation": False,
                "address": address,
                "dns_name": dns,
                "hostname": hostname,
                "pool": pool["slug"],
                "connector": pool["connector_slug"],
                "simulated": True,
                "notice": "Prévisualisation seule : aucune réservation créée.",
            }
        )
    return out


def prepare(
    conn: Conn,
    *,
    hostname: str,
    project_id: UUID | None = None,
    environment_id: UUID | None = None,
    change_id: UUID | None = None,
) -> dict[str, Any]:
    """Prépare une réservation locale (état `prepared`)."""
    pool = default_pool(conn, lock=True)
    used = _used_addresses(conn, pool["id"])
    address = ipam_fake.next_address(used, pool["cidr"])
    if address is None:
        raise ApiError(409, "ipam_exhausted", "aucune adresse libre dans la plage de laboratoire")
    dns = ipam_fake.dns_name_for(hostname, pool["dns_suffix"])
    rid = uuid7()
    conn.execute(
        "INSERT INTO ipam_reservations (id, pool_id, address, dns_name, state, project_id, "
        "environment_id, change_id, preview_only) VALUES (%s, %s, %s::inet, %s, 'prepared', %s, %s, %s, false)",
        (rid, pool["id"], address, dns, project_id, environment_id, change_id),
    )
    return _view(conn, rid)


def reserve(conn: Conn, reservation_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ipam_reservations WHERE id = %s FOR UPDATE", (reservation_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "réservation IPAM introuvable")
    if row["state"] == "reserved":
        return _view(conn, reservation_id)
    if row["state"] != "prepared":
        raise ApiError(409, "invalid_reservation_state", f"impossible de réserver depuis « {row['state']} »")
    conn.execute(
        "UPDATE ipam_reservations SET state = 'reserved', reserved_at = now() WHERE id = %s",
        (reservation_id,),
    )
    audit.record(
        conn,
        actor_type="system",
        actor_id="ipam",
        action="ipam.reserve",
        outcome="ok",
        targets={"reservation_id": str(reservation_id), "address": str(row["address"])},
        versions={"dns_name": row["dns_name"]},
    )
    return _view(conn, reservation_id)


def confirm(conn: Conn, reservation_id: UUID, *, job_id: UUID | None = None) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ipam_reservations WHERE id = %s FOR UPDATE", (reservation_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "réservation IPAM introuvable")
    if row["state"] == "confirmed":
        return _view(conn, reservation_id)
    if row["state"] not in ("reserved", "held_uncertain"):
        raise ApiError(409, "invalid_reservation_state", f"impossible de confirmer depuis « {row['state']} »")
    conn.execute(
        "UPDATE ipam_reservations SET state = 'confirmed', confirmed_at = now(), job_id = COALESCE(%s, job_id), "
        "hold_reason = NULL WHERE id = %s",
        (job_id, reservation_id),
    )
    return _view(conn, reservation_id)


def release(conn: Conn, reservation_id: UUID, *, reason: str = "released") -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ipam_reservations WHERE id = %s FOR UPDATE", (reservation_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "réservation IPAM introuvable")
    if row["state"] == "released":
        return _view(conn, reservation_id)
    if row["state"] == "held_uncertain":
        raise ApiError(
            409,
            "ipam_held_uncertain",
            "réservation conservée : résultat d'exécution incertain ; réconcilier avant libération",
        )
    if row["state"] == "confirmed":
        # Une IP confirmée (potentiellement en service) ne se libère pas automatiquement ici.
        raise ApiError(
            409,
            "ipam_confirmed",
            "réservation confirmée : libération seulement via un changement approuvé",
        )
    conn.execute(
        "UPDATE ipam_reservations SET state = 'released', released_at = now(), hold_reason = %s WHERE id = %s",
        (reason, reservation_id),
    )
    return _view(conn, reservation_id)


def hold_uncertain(conn: Conn, reservation_id: UUID, *, reason: str, job_id: UUID | None = None) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ipam_reservations WHERE id = %s FOR UPDATE", (reservation_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "réservation IPAM introuvable")
    if row["state"] == "released":
        raise ApiError(409, "already_released", "réservation déjà libérée")
    conn.execute(
        "UPDATE ipam_reservations SET state = 'held_uncertain', hold_reason = %s, "
        "job_id = COALESCE(%s, job_id) WHERE id = %s",
        (reason, job_id, reservation_id),
    )
    return _view(conn, reservation_id)


def for_change(conn: Conn, change_id: UUID) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id FROM ipam_reservations WHERE change_id = %s AND state = ANY(%s) ORDER BY prepared_at",
        (change_id, list(ACTIVE)),
    ).fetchall()
    return [_view(conn, r["id"]) for r in rows]


def ensure_reserved_for_change(
    conn: Conn,
    *,
    change_id: UUID,
    project_id: UUID,
    environment_id: UUID,
    hostnames: list[str],
) -> list[dict[str, Any]]:
    """Garantie d'admission : une réservation `reserved` (ou mieux) par nom d'hôte."""
    existing = {r["dns_name"].split(".")[0]: r for r in for_change(conn, change_id)}
    out: list[dict[str, Any]] = []
    for name in hostnames:
        key = name.lower()
        if key in existing:
            row = existing[key]
            if row["state"] == "prepared":
                row = reserve(conn, UUID(row["id"]))
            out.append(row)
            continue
        prepared = prepare(
            conn,
            hostname=name,
            project_id=project_id,
            environment_id=environment_id,
            change_id=change_id,
        )
        out.append(reserve(conn, UUID(prepared["id"])))
    return out


def _view(conn: Conn, reservation_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT r.*, p.slug AS pool_slug, p.connector_slug
        FROM ipam_reservations r JOIN ipam_pools p ON p.id = r.pool_id
        WHERE r.id = %s
        """,
        (reservation_id,),
    ).fetchone()
    assert row is not None
    return {
        "id": str(row["id"]),
        "address": str(row["address"]),
        "dns_name": row["dns_name"],
        "state": row["state"],
        "pool": row["pool_slug"],
        "connector": row["connector_slug"],
        "preview_only": row["preview_only"],
        "change_id": str(row["change_id"]) if row["change_id"] else None,
        "job_id": str(row["job_id"]) if row["job_id"] else None,
        "hold_reason": row["hold_reason"],
        "simulated": True,
    }
