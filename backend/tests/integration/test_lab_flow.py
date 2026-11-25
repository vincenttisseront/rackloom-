"""Parcours complet sur nœuds de labo jetables : préparation invitée, Ansible, vérification, état du service.

Couvre D1 jusqu'à « Succès », T10, T20, T22 et T41. Ressources locales uniquement : conteneurs
`lab-node` sur un réseau bridge propre au test, détruits à la fin. La personnalisation invitée et
l'attribution d'adresse restent SIMULÉES (vCenter fictif) ; SSH, Ansible et la sonde HTTP sont réels.
Niveau de preuve : testé localement (Terraform CLI, ansible-runner, OpenSSH réels ; aucun vCenter).
"""

from __future__ import annotations

import pytest

import flows
from rackloom import approvals

pytestmark = [pytest.mark.terraform, pytest.mark.ansible, pytest.mark.docker]
API = "/api/v1"


def _approved_and_applied(conn, demo, worker, **need_kw):
    c = flows.create_and_submit(conn, demo, **need_kw)
    assert worker.drain(kinds=["tf_plan"]) == ["tf_plan"]
    flows.approve(conn, demo, c["id"], flows.latest_plan(conn, c["id"]))
    assert worker.drain(kinds=["tf_apply"]) == ["tf_apply"]
    return c


def _hosts(conn, deployment_id):
    conn.commit()
    return conn.execute("SELECT * FROM hosts WHERE deployment_id = %s ORDER BY name", (deployment_id,)).fetchall()


def _step_names(conn, change_id):
    conn.commit()
    return [s["name"] for s in conn.execute(
        "SELECT s.* FROM job_steps s JOIN jobs j ON j.id = s.job_id WHERE j.change_id = %s ORDER BY j.created_at, "
        "s.seq", (change_id,)).fetchall()]


def test_d1_jusqu_au_succes_hote_sain(conn, demo, worker, lab, api):
    lab.start(1)
    c = _approved_and_applied(conn, demo, worker)
    assert worker.drain() == ["guest_ready", "ansible_configure", "verify"]
    change = flows.change(conn, c["id"])
    assert change["state"] == "succeeded", change
    dep = flows.deployment(conn, c["deployment_id"])
    assert dep["service_status"] == "ready" and dep["status"] == "active"
    assert [ck["ok"] for h in dep["service_checks"] for ck in h["checks"]] == [True, True, True]
    host = _hosts(conn, c["deployment_id"])[0]
    assert host["name"] == "vit-dev-web-01" and host["deployment_state"] == "verified"
    steps = _step_names(conn, c["id"])
    assert "Personnalisation invitée (vCenter fictif)" in steps
    assert any(s.startswith("Configuration nginx 1.0.0") for s in steps)

    alice = api("alice")
    sheet = alice.get(f"{API}/hosts/{host['id']}").json()
    assert sheet["health"]["state"] == "healthy"
    assert sheet["packages"]["nginx"] and sheet["facts"]["os"].startswith("Ubuntu 24.04")
    assert sheet["template"] == {"slug": "ubuntu-web", "version": "1.0.0", "title": "Serveur web Ubuntu"}
    assert sheet["deployment"]["service_status_label"] == "Prêt"
    assert sheet["vcenter"]["simulated"] is True and sheet["address_simulated"] is True
    view = alice.get(f"{API}/changes/{c['id']}").json()
    assert view["state_label"] == "Succès" and view["deployment"]["service_status"] == "ready"
    # T10 : journaux et observations hors périmètre → 404.
    chloe = api("chloe")
    assert chloe.get(f"{API}/hosts/{host['id']}/observations?kind=journal").status_code == 404
    assert chloe.get(f"{API}/hosts/{host['id']}").status_code == 404
    assert alice.get(f"{API}/hosts/{host['id']}/observations?kind=journal").status_code == 200


def test_t41_nginx_arrete_avant_verification(conn, demo, worker, lab):
    lab.start(1)
    c = _approved_and_applied(conn, demo, worker)
    assert worker.drain(kinds=["guest_ready", "ansible_configure"]) == ["guest_ready", "ansible_configure"]
    host = _hosts(conn, c["deployment_id"])[0]
    lab.exec(lab.node_of(host["address"]), "sh", "-c", "/etc/init.d/nginx stop; pkill -x nginx || true")
    assert worker.drain(kinds=["verify"]) == ["verify"]
    verify = flows.jobs_of(conn, c["id"], "verify")[0]
    assert verify["state"] == "succeeded"
    dep = flows.deployment(conn, c["deployment_id"])
    assert dep["service_status"] == "created_not_ready"
    assert "sonde HTTP en échec" in dep["service_reason"]
    change = flows.change(conn, c["id"])
    assert change["state"] == "partially_succeeded"


def test_t41_noeud_injoignable_verification_indisponible(conn, demo, worker, lab):
    lab.start(1)
    c = _approved_and_applied(conn, demo, worker)
    assert worker.drain(kinds=["guest_ready", "ansible_configure"]) == ["guest_ready", "ansible_configure"]
    lab.stop(0)
    assert worker.drain(kinds=["verify"]) == ["verify"]
    dep = flows.deployment(conn, c["deployment_id"])
    assert dep["service_status"] == "verification_unavailable"
    assert dep["service_reason"] == "vérification indisponible"
    assert flows.change(conn, c["id"])["state"] == "partially_succeeded"


def test_t20_cle_d_hote_inconnue(conn, demo, worker, lab, worker_env):
    lab.start(1)
    c = _approved_and_applied(conn, demo, worker)
    lab.regenerate_host_key(0)
    assert worker.drain() == ["guest_ready", "verify"]
    host = _hosts(conn, c["deployment_id"])[0]
    assert host["deployment_state"] == "unreachable"
    obs = conn.execute("SELECT data FROM host_observations WHERE host_id = %s AND kind = 'ssh'",
                       (host["id"],)).fetchone()
    assert "clé d'hôte" in obs["data"]["error"]
    dep = flows.deployment(conn, c["deployment_id"])
    assert dep["service_status"] == "created_not_ready"
    assert dep["service_reason"] == "VM créée, configuration incomplète"
    # Aucune suppression : la VM de labo existe toujours.
    assert (worker_env / "demo-infra" / str(c["deployment_id"]) / "vit-dev-web-01.json").exists()


def test_t22_echec_partiel_puis_reprise_ciblee(conn, demo, worker, lab):
    lab.start(2)
    c = _approved_and_applied(conn, demo, worker, quantity=2)
    assert worker.drain(kinds=["guest_ready"]) == ["guest_ready"]
    first, second = _hosts(conn, c["deployment_id"])
    broken = lab.node_of(second["address"])
    lab.exec(broken, "mv", "/etc/apt/sources.list.d/rackloom-lab.list", "/root/rackloom-lab.list")
    assert worker.drain() == ["ansible_configure", "verify"]
    dep = flows.deployment(conn, c["deployment_id"])
    assert dep["service_status"] == "created_not_ready"
    assert dep["service_reason"] == "VM créée, configuration incomplète"
    assert flows.change(conn, c["id"])["state"] == "partially_succeeded"
    states = {h["name"]: h["deployment_state"] for h in _hosts(conn, c["deployment_id"])}
    assert states == {"vit-dev-web-01": "verified", "vit-dev-web-02": "config_failed"}

    lab.exec(broken, "mv", "/root/rackloom-lab.list", "/etc/apt/sources.list.d/rackloom-lab.list")
    retry = approvals.request_retry(conn, demo.principal("alice"), c["id"], {"step": "ansible_configure"})
    conn.commit()
    assert retry["host_ids"] == [str(second["id"])]
    approvals.decide(conn, demo.principal("bruno"), c["id"], {
        "retry_id": str(retry["retry_id"]), "bundle_digest": retry["bundle_digest"], "decision": "approved"})
    conn.commit()
    assert worker.drain() == ["ansible_configure", "verify"]
    configure = flows.jobs_of(conn, c["id"], "ansible_configure")[-1]
    assert configure["payload"]["host_ids"] == [str(second["id"])]
    assert flows.change(conn, c["id"])["state"] == "succeeded"
    assert flows.deployment(conn, c["deployment_id"])["service_status"] == "ready"
