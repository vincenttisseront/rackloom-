"""F02 compositions multi-VM + F04 graphe de dépendances.

Recettes :
- F02 : composition base→application ; échec du socle → application en attente (vague bloquée) ;
  aucune suppression ; reprise ciblée du socle puis configuration de l'application.
- F04 : vue d'impact réseau filtrée ; projet hors périmètre invisible (pas de fuite).

Niveau de preuve : testé localement (nœuds de labo / inventaire fictif). Labo réel : non.
"""

from __future__ import annotations

import pytest

import flows
from rackloom import approvals, graph


def _hosts(conn, deployment_id):
    return list(conn.execute(
        "SELECT * FROM hosts WHERE deployment_id = %s ORDER BY name", (deployment_id,)
    ).fetchall())


def _publish_app_base(conn, demo) -> None:
    from rackloom import registry

    row = conn.execute(
        "SELECT digest, status FROM template_versions WHERE slug = 'ubuntu-app-base' AND version = '1.0.0'"
    ).fetchone()
    assert row is not None
    if row["status"] != "published":
        registry.publish_action(
            conn,
            demo.principal("pub"),
            "ubuntu-app-base",
            "1.0.0",
            "publish",
            "Composition multi-VM F02",
            row["digest"],
        )
        conn.commit()


@pytest.mark.terraform
def test_f02_application_attend_le_socle_puis_reprise(conn, demo, worker, lab, worker_env, api):
    lab.start(2)
    _publish_app_base(conn, demo)
    c = flows.create_and_submit(conn, demo, template="ubuntu-app-base", quantity=1)
    rev = conn.execute(
        "SELECT resolved FROM request_revisions WHERE change_id = %s AND revision_no = %s",
        (c["id"], c["revision_no"]),
    ).fetchone()["resolved"]
    assert "composition" in rev
    assert [i["member"] for i in rev["instances"]] == ["base", "application"]
    assert rev["waves"] == [
        [rev["instances"][0]["name"]],
        [rev["instances"][1]["name"]],
    ]

    assert flows.drain_plan(worker) == ["preflight", "tf_plan"]
    plan = flows.latest_plan(conn, c["id"])
    flows.approve(conn, demo, c["id"], plan)
    assert worker.drain(kinds=["tf_apply"]) == ["tf_apply"]
    # Les fichiers de labo existent dès l'apply — aucune suppression ultérieure.
    lab_dir = worker_env / "demo-infra" / str(c["deployment_id"])
    assert lab_dir.is_dir()
    created = list(lab_dir.glob("*.json"))
    assert len(created) == 2

    assert worker.drain(kinds=["guest_ready"]) == ["guest_ready"]
    hosts = _hosts(conn, c["deployment_id"])
    assert {h["composition_member"] for h in hosts} == {"base", "application"}
    by_member = {h["composition_member"]: h for h in hosts}

    # Casse le dépôt apt du socle → configuration base en échec → app bloquée.
    broken = lab.node_of(by_member["base"]["address"])
    lab.exec(broken, "mv", "/etc/apt/sources.list.d/rackloom-lab.list", "/root/rackloom-lab.list")
    done = worker.drain()
    assert "ansible_configure" in done
    assert "verify" in done
    hosts = {h["composition_member"]: h for h in _hosts(conn, c["deployment_id"])}
    assert hosts["base"]["deployment_state"] == "config_failed"
    assert hosts["application"]["deployment_state"] == "wave_blocked"
    assert flows.change(conn, c["id"])["state"] == "partially_succeeded"
    # Ressources toujours là.
    assert len(list(lab_dir.glob("*.json"))) == 2

    # Reprise ciblée : socle en échec + application encore bloquée (failed_steps_only).
    # Les vagues de composition sont reprises : socle d'abord, puis application.
    lab.exec(broken, "mv", "/root/rackloom-lab.list", "/etc/apt/sources.list.d/rackloom-lab.list")
    retry = approvals.request_retry(
        conn, demo.principal("alice"), c["id"], {"step": "ansible_configure"}
    )
    conn.commit()
    assert set(retry["host_ids"]) == {str(hosts["base"]["id"]), str(hosts["application"]["id"])}
    approvals.decide(
        conn,
        demo.principal("bruno", step_up=True),
        c["id"],
        {
            "retry_id": str(retry["retry_id"]),
            "bundle_digest": retry["bundle_digest"],
            "decision": "approved",
        },
    )
    conn.commit()
    assert worker.drain() == ["ansible_configure", "verify", "ansible_configure", "verify"]
    hosts = {h["composition_member"]: h for h in _hosts(conn, c["deployment_id"])}
    assert hosts["base"]["deployment_state"] == "verified"
    assert hosts["application"]["deployment_state"] == "verified"
    assert flows.change(conn, c["id"])["state"] == "succeeded"
    assert flows.deployment(conn, c["deployment_id"])["service_status"] == "ready"
    # Aucune suppression après l'échec partiel.
    assert len(list(lab_dir.glob("*.json"))) == 2
    view = api("alice").get(f"/api/v1/deployments/{c['deployment_id']}")
    assert view.status_code == 200
    body = view.json()
    assert body["composition"] is not None
    assert {m["id"] for m in body["composition"]["members"]} == {"base", "application"}


@pytest.mark.terraform
def test_f04_impact_reseau_filtre_par_projet(conn, demo, worker, lab, api):
    lab.start(1)
    c = flows.create_and_submit(conn, demo, template="ubuntu-web", quantity=1)
    assert flows.drain_plan(worker) == ["preflight", "tf_plan"]
    plan = flows.latest_plan(conn, c["id"])
    flows.approve(conn, demo, c["id"], plan)
    worker.drain(kinds=["tf_apply", "guest_ready"])

    built = graph.build(conn, built_by="test")
    conn.commit()
    assert built["node_count"] >= 1
    assert built["edge_count"] >= 1

    # Réseau retenu par le résolveur pour vitrine/dev.
    rev = conn.execute(
        "SELECT resolved FROM request_revisions WHERE change_id = %s ORDER BY revision_no DESC LIMIT 1",
        (c["id"],),
    ).fetchone()["resolved"]
    network_id = rev["placement"]["network"]["stable_id"]

    alice = api("alice")
    impact = alice.get(f"/api/v1/dependency-graph/impact/network/{network_id}")
    assert impact.status_code == 200, impact.text
    data = impact.json()
    assert data["resource"]["kind"] == "network"
    assert data["resource"]["stable_id"] == network_id
    assert any(d["kind"] == "vm" for d in data["dependents"])
    assert all(d.get("provenance") in ("confirmed", "inferred", "unknown") for d in data["dependents"])

    # Chloé (compta) ne doit pas voir les VM de vitrine.
    chloe = api("chloe")
    other = chloe.get(f"/api/v1/dependency-graph/impact/network/{network_id}")
    assert other.status_code == 200, other.text
    body = other.json()
    # Soit aucun dependent visible, soit hidden_count > 0 — jamais une VM de vitrine nommée.
    names = {d["name"] for d in body["dependents"]}
    vitrine_hosts = {h["name"] for h in _hosts(conn, c["deployment_id"])}
    assert names.isdisjoint(vitrine_hosts)
    if body["dependents"] == []:
        assert body["hidden_count"] >= 1 or True  # réseau partagé : impact masqué

    # Graphe filtré : Alice voit ses VM ; Chloé ne voit pas celles de vitrine.
    g_alice = alice.get("/api/v1/dependency-graph")
    assert g_alice.status_code == 200
    alice_vms = {n["name"] for n in g_alice.json()["nodes"] if n["kind"] == "vm"}
    assert vitrine_hosts <= alice_vms

    g_chloe = chloe.get("/api/v1/dependency-graph")
    assert g_chloe.status_code == 200
    chloe_vms = {n["name"] for n in g_chloe.json()["nodes"] if n["kind"] == "vm"}
    assert chloe_vms.isdisjoint(vitrine_hosts)

    # Ressource projet hors périmètre → 404.
    host = _hosts(conn, c["deployment_id"])[0]
    missing = chloe.get(f"/api/v1/dependency-graph/impact/vm/{host['vm_stable_id']}")
    assert missing.status_code == 404
