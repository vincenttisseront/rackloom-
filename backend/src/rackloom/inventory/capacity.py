"""F05 : contrôle de capacité sur le placement (mesure, pas réservation).

Lit les attributs d'inventaire du cluster et du datastore proposés. Affiche marge et âge.
Le contrôle est rejoué à l'admission (R110) avant toute écriture.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..db import Conn, Jsonb
from ..policy import BUILTIN_RULES
from . import latest_snapshot


def settings() -> dict[str, Any]:
    platform = BUILTIN_RULES["platform"]
    return {
        "max_age_hours": int(platform.get("capacity_max_age_hours", platform["inventory_max_age_hours"])),
        # Marge minimale restante après allocation (points de base de la capacité totale).
        "min_free_bps": int(platform.get("capacity_min_free_bps", 500)),
    }


def _attrs(item: dict[str, Any] | None) -> dict[str, Any]:
    return dict(item.get("attributes") or {}) if item else {}


def _num(attrs: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in attrs and attrs[key] is not None:
            try:
                return int(attrs[key])
            except (TypeError, ValueError):
                return None
    return None


def load_items(conn: Conn, stable_ids: list[str]) -> dict[str, dict[str, Any]]:
    snap = latest_snapshot(conn)
    if snap is None:
        return {}
    rows = conn.execute(
        "SELECT kind, stable_id, name, attributes, observed_at FROM inventory_items "
        "WHERE snapshot_id = %s AND stable_id = ANY(%s::text[])",
        (snap["id"], stable_ids),
    ).fetchall()
    return {r["stable_id"]: dict(r) for r in rows}


def demand_from_instances(instances: list[dict[str, Any]]) -> dict[str, int]:
    """Besoin agrégé des instances à créer (pas les existantes)."""
    vcpu = memory_mb = disk_gb = 0
    for inst in instances:
        if inst.get("existing"):
            continue
        vcpu += int(inst.get("vcpu") or 0)
        memory_mb += int(inst.get("memory_mb") or 0)
        disk_gb += int(inst.get("disk_gb") or 0)
    return {"vcpu": vcpu, "memory_mb": memory_mb, "disk_gb": disk_gb}


def evaluate(
    conn: Conn,
    *,
    cluster_id: str,
    datastore_id: str,
    demand: dict[str, int],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mesure la capacité disponible vs le besoin. Ne réserve rien."""
    now = now or datetime.now(UTC)
    cfg = settings()
    items = load_items(conn, [cluster_id, datastore_id])
    cluster = items.get(cluster_id)
    datastore = items.get(datastore_id)
    observed_candidates = [i["observed_at"] for i in items.values() if i.get("observed_at")]
    observed_at = min(observed_candidates) if observed_candidates else None
    age_seconds = int((now - observed_at).total_seconds()) if observed_at else None
    stale = observed_at is None or age_seconds is None or age_seconds > cfg["max_age_hours"] * 3600

    resources: list[dict[str, Any]] = []
    ok = not stale
    refusal: dict[str, Any] | None = None

    def check(
        resource: str,
        target: dict[str, Any] | None,
        free_keys: tuple[str, ...],
        total_keys: tuple[str, ...],
        need: int,
        unit: str,
    ) -> None:
        nonlocal ok, refusal
        attrs = _attrs(target)
        free = _num(attrs, *free_keys)
        total = _num(attrs, *total_keys)
        entry: dict[str, Any] = {
            "resource": resource,
            "target": target["name"] if target else None,
            "stable_id": target["stable_id"] if target else None,
            "free": free,
            "total": total,
            "need": need,
            "unit": unit,
            "reservation": False,
            "ok": True,
        }
        if free is None or total is None or total <= 0:
            entry["ok"] = False
            entry["code"] = "capacity_unknown"
            ok = False
            refusal = refusal or {
                "code": "capacity_unknown",
                "resource": resource,
                "reason": f"mesure de capacité absente pour {resource}",
            }
        else:
            remaining = free - need
            margin_required = (total * cfg["min_free_bps"]) // 10_000
            entry["remaining_after"] = remaining
            entry["margin_required"] = margin_required
            if remaining < margin_required:
                entry["ok"] = False
                entry["code"] = "capacity_below_threshold"
                ok = False
                refusal = refusal or {
                    "code": "capacity_below_threshold",
                    "resource": resource,
                    "target": target["name"],
                    "reason": (
                        f"{resource} insuffisant sur « {target['name']} » : "
                        f"libre {free} {unit}, besoin {need}, marge minimale {margin_required}"
                    ),
                }
        resources.append(entry)

    check(
        "cpu",
        cluster,
        ("cpu_mhz_free", "cpu_free_mhz"),
        ("cpu_mhz_total", "cpu_total_mhz"),
        demand.get("vcpu", 0) * 1000,  # 1 vCPU ≈ 1000 MHz dans le modèle de labo
        "MHz",
    )
    check(
        "memory",
        cluster,
        ("memory_mb_free",),
        ("memory_mb_total",),
        demand.get("memory_mb", 0),
        "MiB",
    )
    check(
        "storage",
        datastore,
        ("free_gb",),
        ("capacity_gb",),
        demand.get("disk_gb", 0),
        "GiB",
    )

    if stale:
        ok = False
        refusal = {
            "code": "capacity_stale",
            "resource": "inventory",
            "reason": "les mesures de capacité sont trop anciennes ou absentes",
        }

    return {
        "ok": ok,
        "stale": stale,
        "observed_at": observed_at.isoformat() if observed_at else None,
        "age_seconds": age_seconds,
        "simulated": True,
        "reservation": False,
        "notice": "Mesure d'inventaire, pas une réservation.",
        "demand": demand,
        "resources": resources,
        "refusal": refusal,
        "snapshot_id": str(latest_snapshot(conn)["id"]) if latest_snapshot(conn) else None,
    }


def evaluate_resolution(conn: Conn, resolution: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    placement = resolution.get("placement") or {}
    cluster_id = (placement.get("cluster") or {}).get("stable_id")
    datastore_id = (placement.get("datastore") or {}).get("stable_id")
    if not cluster_id or not datastore_id:
        return {
            "ok": False,
            "stale": True,
            "reservation": False,
            "refusal": {"code": "capacity_unknown", "resource": "placement", "reason": "placement incomplet"},
            "resources": [],
        }
    demand = demand_from_instances(list(resolution.get("instances") or []))
    return evaluate(conn, cluster_id=cluster_id, datastore_id=datastore_id, demand=demand, now=now)


def patch_item_attributes(conn: Conn, stable_id: str, updates: dict[str, Any]) -> None:
    """Aide de test : met à jour les attributs de l'élément dans le dernier instantané."""
    snap = latest_snapshot(conn)
    if snap is None:
        raise RuntimeError("aucun instantané d'inventaire")
    row = conn.execute(
        "SELECT attributes FROM inventory_items WHERE snapshot_id = %s AND stable_id = %s FOR UPDATE",
        (snap["id"], stable_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"élément {stable_id} absent")
    attrs = dict(row["attributes"] or {})
    attrs.update(updates)
    conn.execute(
        "UPDATE inventory_items SET attributes = %s WHERE snapshot_id = %s AND stable_id = %s",
        (Jsonb(attrs), snap["id"], stable_id),
    )
