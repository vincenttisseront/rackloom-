"""F09 : détection de dérive (lecture seule).

Fichier de labo modifié hors Rackloom → dérive datée avec source ; inventaire/vCenter
indisponible → observation_unavailable (pas une dérive). Aucune correction automatique.
Niveau de preuve : testé localement.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import flows
from rackloom import drift, inventory
from rackloom.config import get_settings


@pytest.mark.terraform
def test_f09_modification_hors_rackloom_detectee(conn, demo, worker, worker_env, api):
    c = flows.create_and_submit(conn, demo)
    assert flows.drain_plan(worker) == ["preflight", "tf_plan"]
    plan = flows.latest_plan(conn, c["id"])
    res = flows.approve(conn, demo, c["id"], plan)
    assert worker.drain(kinds=["tf_apply"]) == ["tf_apply"]
    assert flows.job(conn, res["job_id"])["state"] == "succeeded"

    # Guest ready crée les hosts.
    worker.drain(kinds=["guest_ready"])
    dep_id = c["deployment_id"]
    files = list((worker_env / "demo-infra" / str(dep_id)).glob("*.json"))
    assert files, "fichier de labo attendu"
    path: Path = files[0]
    data = json.loads(path.read_text(encoding="utf-8"))
    original = data["memory_mb"]
    data["memory_mb"] = int(original) + 512
    path.write_text(json.dumps(data), encoding="utf-8")

    result = drift.scan_deployment(conn, deployment_id=dep_id, workspaces_parent=worker_env)
    conn.commit()
    assert result["correction"] is False
    assert result["status"] == "drift"
    assert any(f["kind"] == "drift" and f["attribute"] == "memory_mb" for f in result["findings"])
    finding = next(f for f in result["findings"] if f["attribute"] == "memory_mb")
    assert finding["source"] == "outside_rackloom"
    assert finding["expected"] == str(original)
    assert finding["observed"] == str(int(original) + 512)
    assert finding["detected_at"]

    view = api("alice").get(f"/api/v1/deployments/{dep_id}/drift")
    assert view.status_code == 200
    assert view.json()["status"] == "drift"
    assert view.json()["correction"] is False


@pytest.mark.terraform
def test_f09_vcenter_indisponible_pas_une_derive(conn, demo, worker, worker_env):
    c = flows.create_and_submit(conn, demo)
    assert flows.drain_plan(worker) == ["preflight", "tf_plan"]
    plan = flows.latest_plan(conn, c["id"])
    flows.approve(conn, demo, c["id"], plan)
    worker.drain(kinds=["tf_apply", "guest_ready"])

    vs = conn.execute("SELECT id FROM connectors WHERE slug = 'vsphere-fake'").fetchone()
    inventory.record_failure(conn, vs["id"], "vCenter fictif arrêté (test F09)")
    # Masque les snapshots OK pour simuler une observation indisponible.
    conn.execute("UPDATE inventory_snapshots SET status = 'error' WHERE status = 'ok'")
    conn.commit()

    result = drift.scan_deployment(
        conn,
        deployment_id=c["deployment_id"],
        workspaces_parent=worker_env,
    )
    conn.commit()
    assert result["status"] == "observation_unavailable"
    assert result["correction"] is False
    assert all(f["kind"] == "observation_unavailable" for f in result["findings"])
    assert any(f["source"] == "vcenter" for f in result["findings"])
    assert not any(f["kind"] == "drift" for f in result["findings"])
    _ = get_settings  # settings disponibles pour chemins de labo
