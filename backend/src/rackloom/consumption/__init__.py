"""F29 : consommation par équipe.

Présente les ressources allouées (déploiements actifs), mesurées (inventaire
quand les attributs d'usage existent) et estimées (demandes en cours résolues
mais pas encore allouées). Distingue explicitement ces trois natures.

Aucun montant monétaire sans tarif déclaré dans `pricing_rates`.
Les totaux alloués sont réconciliables avec l'inventaire (VM du projet).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .. import inventory, projects
from ..changes.transitions import BEFORE_EXECUTION
from ..db import Conn
from ..identity.authz import Principal
from ..ids import uuid7

RESOURCES = ("vcpu", "memory_mb", "disk_gb")
RESOURCE_UNITS = {"vcpu": "vCPU", "memory_mb": "MiB", "disk_gb": "GiB"}
# Attributs d'usage observés (mesure). Absents → pas de mesure inventée.
MEASURED_KEYS = {
    "vcpu": ("vcpu_used", "cpu_used"),
    "memory_mb": ("memory_mb_used", "memory_used_mb"),
    "disk_gb": ("disk_gb_used", "disk_used_gb"),
}
# Attributs de capacité observée côté inventaire (réconciliation avec l'allocation).
INVENTORY_ALLOC_KEYS = {
    "vcpu": ("vcpu",),
    "memory_mb": ("memory_mb",),
    "disk_gb": ("disk_gb",),
}
# Demandes avant exécution aboutie : révision résolue = estimation, pas allocation.
ESTIMATE_STATES = frozenset(BEFORE_EXECUTION | {"approved", "blocked"})


def _num(attrs: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key in attrs and attrs[key] is not None:
            try:
                return int(attrs[key])
            except (TypeError, ValueError):
                return None
    return None


def _empty_totals() -> dict[str, int]:
    return {r: 0 for r in RESOURCES}


def _add_instance(totals: dict[str, int], inst: dict[str, Any]) -> None:
    for resource in RESOURCES:
        raw = inst.get(resource)
        if raw is None:
            continue
        try:
            totals[resource] += int(raw)
        except (TypeError, ValueError):
            continue


def upsert_rate(
    conn: Conn,
    *,
    resource: str,
    amount_minor: int,
    currency: str = "EUR",
    billing_period: str = "month",
    project_id: UUID | None = None,
    label: str = "",
    simulated: bool = True,
    created_by: UUID | None = None,
) -> dict[str, Any]:
    """Déclare ou remplace un tarif actif (tests / administration)."""
    if resource not in RESOURCES:
        raise ValueError(f"ressource inconnue : {resource}")
    if amount_minor < 0:
        raise ValueError("montant négatif refusé")
    # Clôture l'éventuel tarif actif concurrent (même ressource / période / projet).
    if project_id is None:
        conn.execute(
            "UPDATE pricing_rates SET effective_until = now() "
            "WHERE project_id IS NULL AND resource = %s AND billing_period = %s "
            "AND effective_until IS NULL",
            (resource, billing_period),
        )
    else:
        conn.execute(
            "UPDATE pricing_rates SET effective_until = now() "
            "WHERE project_id = %s AND resource = %s AND billing_period = %s "
            "AND effective_until IS NULL",
            (project_id, resource, billing_period),
        )
    row = conn.execute(
        """
        INSERT INTO pricing_rates (
            id, project_id, resource, billing_period, amount_minor, currency,
            label, simulated, created_by
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            uuid7(),
            project_id,
            resource,
            billing_period,
            amount_minor,
            currency.upper(),
            (label or "")[:120],
            simulated,
            created_by,
        ),
    ).fetchone()
    assert row is not None
    return dict(row)


def active_rates(conn: Conn, project_id: UUID) -> dict[str, dict[str, Any]]:
    """Tarifs actifs : projet d'abord, sinon plateforme. Une entrée par ressource."""
    rows = conn.execute(
        """
        SELECT * FROM pricing_rates
        WHERE effective_until IS NULL
          AND (project_id IS NULL OR project_id = %s)
          AND effective_from <= now()
        ORDER BY project_id NULLS FIRST, resource
        """,
        (project_id,),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        # Plateforme d'abord (NULLS FIRST), puis écrasement éventuel par un tarif projet.
        out[row["resource"]] = dict(row)
    return out


def _allocated_from_deployments(conn: Conn, project_id: UUID) -> tuple[dict[str, int], int, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT d.id, d.template_slug, d.instances, e.slug AS environment_slug
        FROM deployments d
        JOIN environments e ON e.id = d.environment_id
        WHERE d.project_id = %s AND d.status = 'active'
        ORDER BY d.created_at
        """,
        (project_id,),
    ).fetchall()
    totals = _empty_totals()
    sources: list[dict[str, Any]] = []
    host_count = 0
    for dep in rows:
        for inst in list(dep["instances"] or []):
            if not isinstance(inst, dict):
                continue
            _add_instance(totals, inst)
            host_count += 1
            sources.append(
                {
                    "deployment_id": str(dep["id"]),
                    "template": dep["template_slug"],
                    "environment": dep["environment_slug"],
                    "name": inst.get("name"),
                    "vcpu": inst.get("vcpu"),
                    "memory_mb": inst.get("memory_mb"),
                    "disk_gb": inst.get("disk_gb"),
                    "kind": "allocation",
                }
            )
    return totals, host_count, sources


def _estimated_from_changes(conn: Conn, project_id: UUID) -> tuple[dict[str, int], int, list[dict[str, Any]]]:
    """Révisions résolues de demandes non encore abouties → estimation (pas allocation)."""
    rows = conn.execute(
        """
        SELECT c.id, c.seq, c.state, c.deployment_id, r.resolved, r.id AS revision_id
        FROM changes c
        JOIN LATERAL (
            SELECT id, resolved FROM request_revisions
            WHERE change_id = c.id AND outcome = 'resolved'
            ORDER BY created_at DESC LIMIT 1
        ) r ON true
        WHERE c.project_id = %s AND c.state = ANY(%s)
        ORDER BY c.seq
        """,
        (project_id, list(ESTIMATE_STATES)),
    ).fetchall()
    totals = _empty_totals()
    sources: list[dict[str, Any]] = []
    host_count = 0
    for row in rows:
        # Déjà alloué sur un déploiement actif : ne pas double-compter en estimation.
        if row["deployment_id"] is not None:
            dep = conn.execute("SELECT status FROM deployments WHERE id = %s", (row["deployment_id"],)).fetchone()
            if dep is not None and dep["status"] == "active":
                continue
        resolved = row["resolved"] or {}
        for inst in list(resolved.get("instances") or []):
            if not isinstance(inst, dict) or inst.get("existing"):
                continue
            _add_instance(totals, inst)
            host_count += 1
            sources.append(
                {
                    "change_id": str(row["id"]),
                    "ref": f"CHG-{int(row['seq']):04d}",
                    "state": row["state"],
                    "revision_id": str(row["revision_id"]),
                    "name": inst.get("name"),
                    "vcpu": inst.get("vcpu"),
                    "memory_mb": inst.get("memory_mb"),
                    "disk_gb": inst.get("disk_gb"),
                    "kind": "estimation",
                }
            )
    return totals, host_count, sources


def _project_inventory_vms(conn: Conn, project_id: UUID) -> list[dict[str, Any]]:
    """VM d'inventaire rattachées au projet : hôtes enrôlés + octrois `vm`."""
    snap = inventory.latest_snapshot(conn)
    if snap is None:
        return []
    host_ids = [
        r["vm_stable_id"]
        for r in conn.execute("SELECT vm_stable_id FROM hosts WHERE project_id = %s", (project_id,)).fetchall()
    ]
    granted = inventory.granted_items(conn, project_id, kind="vm")
    by_id: dict[str, dict[str, Any]] = {}
    for g in granted:
        by_id[g["stable_id"]] = dict(g)
    if host_ids:
        rows = conn.execute(
            "SELECT kind, stable_id, name, attributes, observed_at FROM inventory_items "
            "WHERE snapshot_id = %s AND kind = 'vm' AND stable_id = ANY(%s::text[])",
            (snap["id"], host_ids),
        ).fetchall()
        for row in rows:
            by_id[row["stable_id"]] = dict(row)
    return list(by_id.values())


def _inventory_sides(
    vms: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int | None], datetime | None]:
    """Totaux inventaire (capacité allouée observée) et mesures d'usage si présentes."""
    alloc = _empty_totals()
    measured: dict[str, int | None] = {r: None for r in RESOURCES}
    measured_acc = _empty_totals()
    measured_seen = {r: False for r in RESOURCES}
    observed_at: datetime | None = None
    for vm in vms:
        attrs = dict(vm.get("attributes") or {})
        for resource in RESOURCES:
            n = _num(attrs, INVENTORY_ALLOC_KEYS[resource])
            if n is not None:
                alloc[resource] += n
            m = _num(attrs, MEASURED_KEYS[resource])
            if m is not None:
                measured_acc[resource] += m
                measured_seen[resource] = True
        oa = vm.get("observed_at")
        if isinstance(oa, datetime):
            observed_at = min(observed_at, oa) if observed_at else oa
    for resource in RESOURCES:
        if measured_seen[resource]:
            measured[resource] = measured_acc[resource]
    return alloc, measured, observed_at


def _billing(
    allocated: dict[str, int],
    rates: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if not rates:
        return None
    # Une seule devise / période pour le rapport ; mélange → pas de facture partielle inventée.
    currencies = {r["currency"] for r in rates.values()}
    periods = {r["billing_period"] for r in rates.values()}
    if len(currencies) != 1 or len(periods) != 1:
        return None
    currency = next(iter(currencies))
    period = next(iter(periods))
    lines: list[dict[str, Any]] = []
    total = 0
    for resource in RESOURCES:
        rate = rates.get(resource)
        if rate is None:
            continue
        qty = int(allocated.get(resource) or 0)
        amount = qty * int(rate["amount_minor"])
        total += amount
        lines.append(
            {
                "resource": resource,
                "quantity": qty,
                "basis": "allocation",
                "kind": "estimation",
                "unit_amount_minor": int(rate["amount_minor"]),
                "amount_minor": amount,
                "rate_id": str(rate["id"]),
                "simulated": bool(rate["simulated"]),
            }
        )
    if not lines:
        return None
    return {
        "currency": currency,
        "period": period,
        "lines": lines,
        "total_minor": total,
        "kind": "estimation",
        "notice": "Montants calculés uniquement à partir des tarifs déclarés (base : allocation).",
        "simulated": any(line["simulated"] for line in lines),
    }


def project_report(conn: Conn, principal: Principal, project_ref: str) -> dict[str, Any]:
    """Rapport de consommation du projet (périmètre authentifié)."""
    project = projects.get_project(conn, principal, project_ref)
    project_id = project["id"]
    allocated, alloc_hosts, alloc_sources = _allocated_from_deployments(conn, project_id)
    estimated, est_hosts, est_sources = _estimated_from_changes(conn, project_id)
    vms = _project_inventory_vms(conn, project_id)
    inv_alloc, measured_vals, measured_at = _inventory_sides(vms)

    # Réconciliation : totaux d'allocation vs inventaire (mêmes métriques).
    delta = {r: allocated[r] - inv_alloc[r] for r in RESOURCES}
    host_rows = conn.execute(
        "SELECT vm_stable_id, name FROM hosts WHERE project_id = %s ORDER BY name",
        (project_id,),
    ).fetchall()
    host_ids = {h["vm_stable_id"] for h in host_rows}
    inv_ids = {v["stable_id"] for v in vms}
    matched = sorted(host_ids & inv_ids)
    missing = sorted(host_ids - inv_ids)
    inventory_only = sorted(inv_ids - host_ids)
    reconciled = (all(delta[r] == 0 for r in RESOURCES) and not missing and bool(host_ids or inv_ids)) or (
        not host_ids and not inv_ids
    )

    rates = active_rates(conn, project_id)
    billing = _billing(allocated, rates)

    resources_view: list[dict[str, Any]] = []
    for resource in RESOURCES:
        entry: dict[str, Any] = {
            "resource": resource,
            "unit": RESOURCE_UNITS[resource],
            "allocated": {
                "value": allocated[resource],
                "kind": "allocation",
            },
            "estimated": {
                "value": estimated[resource],
                "kind": "estimation",
            },
            "measured": None,
        }
        if measured_vals[resource] is not None:
            entry["measured"] = {
                "value": measured_vals[resource],
                "kind": "measurement",
                "observed_at": measured_at.isoformat() if measured_at else None,
            }
        resources_view.append(entry)

    snap = inventory.latest_snapshot(conn)
    return {
        "project": {
            "id": str(project["id"]),
            "slug": project["slug"],
            "name": project["name"],
            "code": project["code"],
        },
        "as_of": datetime.now(UTC).isoformat(),
        "simulated": True,
        "notice": (
            "Allocation = déploiements actifs ; mesure = usage inventaire s'il est présent ; "
            "estimation = demandes résolues non encore allouées. "
            "Aucun montant sans tarif déclaré."
        ),
        "resources": resources_view,
        "totals": {
            "allocated": {**allocated, "hosts": alloc_hosts},
            "measured": {
                **{r: measured_vals[r] for r in RESOURCES},
                "hosts": len(vms) if any(measured_vals[r] is not None for r in RESOURCES) else None,
            },
            "estimated": {**estimated, "hosts": est_hosts},
        },
        "sources": {
            "allocated": alloc_sources,
            "estimated": est_sources,
        },
        "inventory_reconciliation": {
            "ok": reconciled,
            "allocated": {r: allocated[r] for r in RESOURCES},
            "inventory": inv_alloc,
            "delta": delta,
            "matched_hosts": len(matched),
            "missing_from_inventory": missing,
            "inventory_only_vms": inventory_only,
            "snapshot_id": str(snap["id"]) if snap else None,
            "observed_at": snap["observed_at"].isoformat() if snap else None,
            "notice": (
                "Totaux réconciliables avec l'inventaire"
                if reconciled
                else "Écart ou couverture partielle entre allocation et inventaire"
            ),
        },
        "billing": billing,
        "billing_unavailable_reason": None if billing else "no_pricing_rates",
    }


def estimate_cost_for_demand(
    conn: Conn,
    project_id: UUID,
    instances: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Coût estimé pour le comparateur (F52) : seulement si des tarifs existent."""
    totals = _empty_totals()
    for inst in instances:
        if isinstance(inst, dict) and not inst.get("existing"):
            _add_instance(totals, inst)
    rates = active_rates(conn, project_id)
    return _billing(totals, rates)
