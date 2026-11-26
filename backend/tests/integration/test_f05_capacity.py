"""F05 : contrôle de capacité.

Inventaire fictif modifié après approbation → écriture bloquée `capacity_below_threshold`
avec la ressource ; mesure trop ancienne → refus. Jamais présentée comme une réservation.
Niveau de preuve : testé localement (pas de cluster réel).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import flows
from rackloom.inventory import capacity
from rackloom.policy import BUILTIN_VERSION

API = "/api/v1"


def _approved(conn, demo, worker):
    c = flows.create_and_submit(conn, demo)
    assert flows.drain_plan(worker) == ["preflight", "tf_plan"]
    plan = flows.latest_plan(conn, c["id"])
    res = flows.approve(conn, demo, c["id"], plan)
    return c, plan, res["job_id"]


@pytest.mark.terraform
def test_f05_capacite_chute_bloque_avec_ressource(conn, demo, worker, worker_env, api):
    c, plan, job_id = _approved(conn, demo, worker)
    details = api("alice").get(f"{API}/changes/{c['id']}").json()["plan"]["details"]
    assert details["capacity"]["reservation"] is False
    assert "réservation" in details["capacity"]["notice"].lower() or "Mesure" in details["capacity"]["notice"]
    assert details["capacity"]["resources"]

    # Capacité mémoire passée sous le seuil après approbation.
    capacity.patch_item_attributes(
        conn,
        "domain-c10",
        {"memory_mb_free": 100, "cpu_mhz_free": 500},
    )
    conn.commit()

    assert worker.drain(kinds=["tf_apply"]) == ["tf_apply"]
    job = flows.job(conn, job_id)
    assert job["state"] == "blocked"
    assert job["blocked_reason"] == "capacity_below_threshold"
    assert job["dispatched_at"] is None
    assert not list((worker_env / "demo-infra" / str(c["deployment_id"])).glob("*.json"))

    decision = conn.execute(
        "SELECT rules FROM admission_decisions WHERE job_id = %s ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()
    last = decision["rules"][-1]
    assert last["id"] == "R110"
    assert last["code"] == "capacity_below_threshold"
    assert "memory" in last["reason"] or "cl-dev" in last["reason"]

    view = api("alice").get(f"{API}/changes/{c['id']}").json()
    assert view["state"] == "blocked"
    assert BUILTIN_VERSION.startswith("builtin@")


@pytest.mark.terraform
def test_f05_mesure_trop_ancienne_refuse(conn, demo, worker):
    c, plan, job_id = _approved(conn, demo, worker)
    # Vieillit l'instantané au-delà de capacity_max_age_hours.
    old = datetime.now(UTC) - timedelta(hours=48)
    conn.execute("UPDATE inventory_snapshots SET observed_at = %s", (old,))
    conn.execute("UPDATE inventory_items SET observed_at = %s", (old,))
    conn.commit()

    assert worker.drain(kinds=["tf_apply"]) == ["tf_apply"]
    job = flows.job(conn, job_id)
    assert job["state"] == "blocked"
    assert job["blocked_reason"] == "capacity_stale"
    decision = conn.execute(
        "SELECT rules FROM admission_decisions WHERE job_id = %s ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()
    assert decision["rules"][-1]["id"] == "R110"
    assert decision["rules"][-1]["code"] == "capacity_stale"
