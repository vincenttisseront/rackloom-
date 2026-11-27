"""F29 : consommation par équipe.

Totaux alloués réconciliables avec l'inventaire fictif ; aucun montant sans tarif ;
allocation / mesure / estimation distingués.
Niveau de preuve : testé localement (mesures vCenter = labo L2).
"""

from __future__ import annotations

from datetime import UTC, datetime

from rackloom import consumption
from rackloom.db import Jsonb
from rackloom.ids import uuid7
from rackloom.inventory import capacity as capacity_mod

API = "/api/v1"


def _plant_active(
    conn,
    demo,
    *,
    name: str = "vit-dev-web-29",
    vcpu: int = 2,
    memory_mb: int = 4096,
    disk_gb: int = 40,
    vm_stable_id: str = "vm-9001",
):
    project = demo.project("vitrine")
    env = demo.env("vitrine", "dev")
    tpl = conn.execute(
        "SELECT id FROM template_versions WHERE slug = 'ubuntu-web' AND status = 'published' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
    alice = demo.user("alice")
    change_id, stack_id, deployment_id = uuid7(), uuid7(), uuid7()
    conn.execute("SET CONSTRAINTS ALL DEFERRED")
    conn.execute(
        "INSERT INTO changes (id, kind, project_id, environment_id, title, state, created_by) "
        "VALUES (%s, 'create', %s, %s, 'f29', 'succeeded', %s)",
        (change_id, project["id"], env["id"], alice["id"]),
    )
    conn.execute(
        "INSERT INTO stacks (id, slug, project_id, environment_id, backend_kind, backend_ref) "
        "VALUES (%s, %s, %s, %s, 'local_demo', 'f29')",
        (stack_id, f"f29-{stack_id.hex}", project["id"], env["id"]),
    )
    inst = [{"name": name, "size_tier": "standard", "vcpu": vcpu, "memory_mb": memory_mb, "disk_gb": disk_gb}]
    conn.execute(
        "INSERT INTO deployments (id, project_id, environment_id, template_slug, template_version_id, "
        "stack_id, created_by_change_id, owner_id, status, service_status, service_reason, "
        "service_checked_at, service_checks, instances) "
        "VALUES (%s, %s, %s, 'ubuntu-web', %s, %s, %s, %s, 'active', 'ready', %s, %s, %s, %s)",
        (
            deployment_id,
            project["id"],
            env["id"],
            tpl["id"],
            stack_id,
            change_id,
            alice["id"],
            "tous les contrôles de santé ont réussi",
            datetime.now(UTC),
            Jsonb([{"host": name, "state": "checked", "checks": [{"type": "http_probe", "ok": True}]}]),
            Jsonb(inst),
        ),
    )
    conn.execute("UPDATE changes SET deployment_id = %s WHERE id = %s", (deployment_id, change_id))
    conn.execute(
        "INSERT INTO hosts (id, project_id, environment_id, deployment_id, template_version_id, change_id, "
        "stack_id, vm_stable_id, name, deployment_state) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'verified')",
        (
            uuid7(),
            project["id"],
            env["id"],
            deployment_id,
            tpl["id"],
            change_id,
            stack_id,
            vm_stable_id,
            name,
        ),
    )
    return {"deployment_id": deployment_id, "project": project, "change_id": change_id}


def test_f29_sans_tarif_aucun_montant(conn, demo, api):
    report = api("alice").get(f"{API}/projects/vitrine/consumption").json()
    assert report["billing"] is None
    assert report["billing_unavailable_reason"] == "no_pricing_rates"
    assert all(r["allocated"]["kind"] == "allocation" for r in report["resources"])
    assert all(r["estimated"]["kind"] == "estimation" for r in report["resources"])
    # Mesure absente tant que l'inventaire n'expose pas d'usage.
    assert all(r["measured"] is None or r["measured"]["kind"] == "measurement" for r in report["resources"])


def test_f29_totaux_reconciliables_avec_inventaire(conn, demo, api):
    # vm-9001 : 2 / 4096 / 40 dans l'inventaire fictif.
    _plant_active(conn, demo, vcpu=2, memory_mb=4096, disk_gb=40, vm_stable_id="vm-9001", name="vit-f29-a")
    # Seul vm-9001 est hôte ; vm-9002 reste inventory_only → écart volontaire.
    # Pour un match strict, on aligne aussi vm-9002.
    _plant_active(
        conn,
        demo,
        vcpu=2,
        memory_mb=2048,
        disk_gb=30,
        vm_stable_id="vm-9002",
        name="vit-f29-b",
    )
    conn.commit()

    report = api("alice").get(f"{API}/projects/vitrine/consumption").json()
    totals = report["totals"]["allocated"]
    assert totals["vcpu"] == 4
    assert totals["memory_mb"] == 6144
    assert totals["disk_gb"] == 70
    assert totals["hosts"] == 2

    recon = report["inventory_reconciliation"]
    assert recon["inventory"] == {"vcpu": 4, "memory_mb": 6144, "disk_gb": 70}
    assert recon["allocated"] == recon["inventory"]
    assert recon["delta"] == {"vcpu": 0, "memory_mb": 0, "disk_gb": 0}
    assert recon["ok"] is True
    assert recon["matched_hosts"] == 2
    assert recon["missing_from_inventory"] == []


def test_f29_mesure_quand_attributs_presents(conn, demo, api):
    _plant_active(conn, demo, vm_stable_id="vm-9001", name="vit-f29-m")
    capacity_mod.patch_item_attributes(
        conn,
        "vm-9001",
        {"vcpu_used": 1, "memory_mb_used": 1024, "disk_gb_used": 12},
    )
    conn.commit()

    report = api("alice").get(f"{API}/projects/vitrine/consumption").json()
    by_res = {r["resource"]: r for r in report["resources"]}
    assert by_res["vcpu"]["measured"]["kind"] == "measurement"
    assert by_res["vcpu"]["measured"]["value"] == 1
    assert by_res["memory_mb"]["measured"]["value"] == 1024
    assert by_res["disk_gb"]["measured"]["value"] == 12
    assert by_res["vcpu"]["allocated"]["kind"] == "allocation"


def test_f29_tarif_expose_montant_estime(conn, demo, api):
    _plant_active(conn, demo, vcpu=2, memory_mb=4096, disk_gb=40, name="vit-f29-€")
    consumption.upsert_rate(conn, resource="vcpu", amount_minor=500, label="vCPU / mois")
    consumption.upsert_rate(conn, resource="memory_mb", amount_minor=1, label="MiB / mois")
    consumption.upsert_rate(conn, resource="disk_gb", amount_minor=10, label="GiB / mois")
    conn.commit()

    report = api("alice").get(f"{API}/projects/vitrine/consumption").json()
    assert report["billing_unavailable_reason"] is None
    billing = report["billing"]
    assert billing is not None
    assert billing["currency"] == "EUR"
    assert billing["kind"] == "estimation"
    assert all(line["basis"] == "allocation" for line in billing["lines"])
    # 2*500 + 4096*1 + 40*10 = 1000 + 4096 + 400 = 5496
    assert billing["total_minor"] == 5496


def test_f29_hors_perimetre_404(conn, demo, api):
    # Chloé (compta) ne voit pas vitrine.
    res = api("chloe").get(f"{API}/projects/vitrine/consumption")
    assert res.status_code == 404


def test_f29_estimation_demande_plan_ready(conn, demo, api):
    project = demo.project("vitrine")
    env = demo.env("vitrine", "dev")
    alice = demo.user("alice")
    change_id = uuid7()
    conn.execute(
        "INSERT INTO changes (id, kind, project_id, environment_id, title, state, created_by, "
        "current_revision_no) VALUES (%s, 'create', %s, %s, 'f29-est', 'plan_ready', %s, 1)",
        (change_id, project["id"], env["id"], alice["id"]),
    )
    tpl = conn.execute(
        "SELECT id FROM template_versions WHERE slug = 'ubuntu-web' AND status = 'published' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
    conn.execute(
        "INSERT INTO request_versions (id, change_id, version_no, need, provenance, need_sha256, source, created_by) "
        "VALUES (%s, %s, 1, %s, %s, %s, 'api', %s)",
        (
            uuid7(),
            change_id,
            Jsonb({"schema": "rackloom.need/v1", "service": {"template": "ubuntu-web"}, "inputs": {}}),
            Jsonb({}),
            "x" * 64,
            alice["id"],
        ),
    )
    conn.execute(
        "INSERT INTO request_revisions (id, change_id, revision_no, version_no, outcome, template_version_id, "
        "environment_id, policy_bundle_version, resolved, provenance, resolution_digest) "
        "VALUES (%s, %s, 1, 1, 'resolved', %s, %s, 'builtin@1', %s, %s, %s)",
        (
            uuid7(),
            change_id,
            tpl["id"],
            env["id"],
            Jsonb(
                {
                    "instances": [
                        {
                            "name": "vit-dev-web-est",
                            "size_tier": "test_leger",
                            "vcpu": 2,
                            "memory_mb": 3072,
                            "disk_gb": 20,
                        }
                    ]
                }
            ),
            Jsonb({}),
            "y" * 64,
        ),
    )
    conn.execute(
        "UPDATE changes SET current_version_no = 1, current_revision_no = 1 WHERE id = %s",
        (change_id,),
    )
    conn.commit()

    report = api("alice").get(f"{API}/projects/vitrine/consumption").json()
    assert report["totals"]["estimated"]["vcpu"] == 2
    assert report["totals"]["estimated"]["memory_mb"] == 3072
    assert report["totals"]["estimated"]["disk_gb"] == 20
    assert report["sources"]["estimated"]
    assert report["sources"]["estimated"][0]["kind"] == "estimation"
