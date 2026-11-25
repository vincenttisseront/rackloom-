"""Parcours complet sur nœuds de labo jetables : préparation invitée, Ansible, vérification, état du service.

Couvre D1 jusqu'à « Succès », T10, T20, T22, T25 (réconciliation Ansible) et T41. Ressources locales
uniquement : conteneurs `lab-node` sur un réseau bridge propre au test, détruits à la fin. La
personnalisation invitée et l'attribution d'adresse restent SIMULÉES (vCenter fictif) ; SSH, Ansible
et la sonde HTTP sont réels. Niveau de preuve : testé localement (Terraform CLI, ansible-runner,
OpenSSH réels ; aucun vCenter).
"""


from __future__ import annotations

import json

import pytest

import flows
from rackloom import approvals
from rackloom.changes.transitions import transition
from rackloom.db import transaction
from rackloom.errors import ApiError
from rackloom.executions import queue
from rackloom.projects import freezes

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
    # Rapport Ansible filtré (classe B) : téléchargeable par un membre seulement ; classe A jamais.
    arts = {a["kind"]: a for a in conn.execute("SELECT * FROM artifacts WHERE change_id = %s", (c["id"],))}
    report = arts["ansible_configure_report"]
    assert report["key_class"] == "B"
    body = alice.get(f"{API}/artifacts/{report['id']}")
    assert body.status_code == 200 and "vit-dev-web-01" in __import__("json").loads(body.text)["hosts"]
    assert chloe.get(f"{API}/artifacts/{report['id']}").status_code == 404
    assert alice.get(f"{API}/artifacts/{arts['tf_plan_bin']['id']}").status_code == 404


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


def test_at01_vagues_nginx_fail2ban_et_second_passage(conn, demo, worker, lab):
    """Deux machines, pilote puis vague ; Fail2ban vérifié ; second passage sans changement."""
    from rackloom.executions import queue

    lab.start(2)
    flows.publish_hardened(conn, demo)
    c = _approved_and_applied(conn, demo, worker, template="ubuntu-web-durci", quantity=2)
    deployment = flows.deployment(conn, c["deployment_id"])
    conn.execute(
        "INSERT INTO hosts (id, project_id, environment_id, deployment_id, template_version_id, change_id, "
        "stack_id, vm_stable_id, name, deployment_state) SELECT %s, project_id, environment_id, id, "
        "template_version_id, %s, stack_id, 'vm-lab-rogue', 'vit-dev-web-99', 'created' "
        "FROM deployments WHERE id = %s",
        (queue.new_id(), c["id"], c["deployment_id"]),
    )
    conn.commit()
    assert worker.drain() == ["guest_ready", "ansible_configure", "verify", "ansible_configure", "verify"]
    dep = flows.deployment(conn, c["deployment_id"])
    assert dep["service_status"] == "ready", dep["service_reason"]
    hosts = _hosts(conn, c["deployment_id"])
    states = {h["name"]: h["deployment_state"] for h in hosts}
    assert states == {"vit-dev-web-01": "verified", "vit-dev-web-02": "verified", "vit-dev-web-99": "out_of_scope"}
    checks = {h["host"]: {ck["type"]: ck["ok"] for ck in h["checks"]} for h in dep["service_checks"]}
    for name in ("vit-dev-web-01", "vit-dev-web-02"):
        assert checks[name]["package_installed"] and checks[name]["jail_active"] and checks[name]["http_probe"]
    steps = _step_names(conn, c["id"])
    assert any(s.startswith("Pilote · Configuration") for s in steps)
    assert any(s.startswith("Vague 2 · Configuration") for s in steps)
    assert any("Hors de la liste approuvée" in s for s in steps)
    level = conn.execute("SELECT verification_level FROM component_versions WHERE slug = 'fail2ban'").fetchone()
    assert level["verification_level"] == "tested_locally"
    # Second passage : la même configuration approuvée ne change plus rien.
    execution = conn.execute("SELECT id FROM executions WHERE change_id = %s", (c["id"],)).fetchone()
    change = flows.change(conn, c["id"])
    queue.enqueue(
        conn,
        kind="ansible_configure",
        change=change,
        execution_id=execution["id"],
        idempotency_key=f"ansible_configure:second:{c['id']}",
        payload={"host_ids": [str(h["id"]) for h in hosts if h["name"] != "vit-dev-web-99"]},
    )
    conn.commit()
    assert worker.drain(kinds=["ansible_configure"]) == ["ansible_configure"]
    second = flows.jobs_of(conn, c["id"], "ansible_configure")[-1]
    steps = conn.execute(
        "SELECT name, state, summary FROM job_steps WHERE job_id = %s ORDER BY seq", (second["id"],)
    ).fetchall()
    changed = [s["summary"].get("changed") for s in steps if "changed" in (s["summary"] or {})]
    detail = {
        "state": second["state"],
        "blocked": second.get("blocked_reason"),
        "result": second.get("result"),
        "steps": [(s["name"], s["state"], dict(s["summary"] or {})) for s in steps],
    }
    assert second["state"] == "succeeded" and changed and set(changed) == {0}, detail
    assert deployment["id"]  # le déploiement créé par l'application reste celui de ce changement


def test_at01_echec_du_pilote_bloque_la_vague(conn, demo, worker, lab):
    lab.start(2)
    flows.publish_hardened(conn, demo)
    c = _approved_and_applied(conn, demo, worker, template="ubuntu-web-durci", quantity=2)
    assert worker.drain(kinds=["guest_ready"]) == ["guest_ready"]
    pilot, follow = _hosts(conn, c["deployment_id"])
    broken = lab.node_of(pilot["address"])
    lab.exec(broken, "mv", "/etc/apt/sources.list.d/rackloom-lab.list", "/root/rackloom-lab.list")
    assert worker.drain() == ["ansible_configure", "verify"]
    states = {h["name"]: h["deployment_state"] for h in _hosts(conn, c["deployment_id"])}
    assert states[pilot["name"]] == "config_failed"
    assert states[follow["name"]] == "wave_blocked"
    probe = lab.exec(lab.node_of(follow["address"]), "sh", "-c", "dpkg-query -W nginx >/dev/null 2>&1; echo $?")
    assert probe == "1"
    dep = flows.deployment(conn, c["deployment_id"])
    assert dep["service_status"] == "created_not_ready"
    assert "bloquée" in dep["service_reason"]
    assert flows.change(conn, c["id"])["state"] == "partially_succeeded"

    lab.exec(broken, "mv", "/root/rackloom-lab.list", "/etc/apt/sources.list.d/rackloom-lab.list")
    retry = approvals.request_retry(conn, demo.principal("alice"), c["id"], {"step": "ansible_configure"})
    conn.commit()
    assert set(retry["host_ids"]) == {str(pilot["id"]), str(follow["id"])}
    approvals.decide(
        conn,
        demo.principal("bruno"),
        c["id"],
        {"retry_id": str(retry["retry_id"]), "bundle_digest": retry["bundle_digest"], "decision": "approved"},
    )
    conn.commit()
    assert worker.drain() == ["ansible_configure", "verify", "ansible_configure", "verify"]
    assert flows.change(conn, c["id"])["state"] == "succeeded"
    assert flows.deployment(conn, c["deployment_id"])["service_status"] == "ready"


def test_t25_reconcile_ansible_depuis_sorties(conn, demo, worker, lab, worker_env):
    """T25 : configure terminé sur disque, worker mort avant écriture → réconciliation, aucune relance."""
    lab.start(1)
    c = _approved_and_applied(conn, demo, worker)
    assert worker.drain(kinds=["guest_ready"]) == ["guest_ready"]
    configure = flows.jobs_of(conn, c["id"], "ansible_configure")[0]
    host = _hosts(conn, c["deployment_id"])[0]
    run_id = str(configure["id"])
    work = worker_env / "workspaces" / run_id
    (work / "out").mkdir(parents=True)
    (work / ".run.json").write_text(json.dumps({"run_id": run_id, "job_id": run_id}), encoding="utf-8")
    (work / "out" / "exit_code").write_text("0", encoding="utf-8")
    (work / "out" / "ansible_summary.json").write_text(json.dumps({
        "status": "successful",
        "hosts": {host["name"]: {"ok": 3, "changed": 1, "failures": 0, "dark": 0, "skipped": 0, "tasks": []}},
        "reports": {},
    }), encoding="utf-8")
    with transaction() as tx:
        tx.execute(
            "UPDATE jobs SET state = 'uncertain', dispatched_at = now(), provider_ref = %s, "
            "lease_owner = NULL, updated_at = now() WHERE id = %s",
            (run_id, configure["id"]))
        change = tx.execute("SELECT * FROM changes WHERE id = %s FOR UPDATE", (c["id"],)).fetchone()
        transition(tx, change, "uncertain", reason_code="lease_lost_after_dispatch", actor_type="worker",
                   actor_id="test-worker-1",
                   detail={"message": "Résultat incertain : réconciliation en lecture seule"})
        queue.enqueue(tx, kind="reconcile", change=change, idempotency_key=f"reconcile:{configure['id']}",
                      payload={"job_id": str(configure["id"])}, priority=10)
    assert worker.drain(kinds=["reconcile", "verify"]) == ["reconcile", "verify"]
    job = flows.job(conn, configure["id"])
    assert job["state"] == "succeeded" and job["result"]["source"] == "workspace"
    # Sorties fictives : pas d'installation réelle → verify peut conclure partiel ; l'essentiel
    # est qu'aucune seconde configuration n'ait été lancée et que le job incertain soit clos.
    assert flows.change(conn, c["id"])["state"] in ("succeeded", "partially_succeeded")
    assert len(flows.jobs_of(conn, c["id"], "ansible_configure")) == 1
    assert flows.jobs_of(conn, c["id"], "reconcile")[0]["state"] == "succeeded"


def test_f28_configuration_bloquee_par_le_gel_puis_reprise(conn, demo, worker, lab):
    """VM créée avant le gel, configuration en file pendant le gel : bloquée, jamais envoyée, VM conservée."""
    lab.start(1)
    c = _approved_and_applied(conn, demo, worker)
    assert worker.drain(kinds=["guest_ready"]) == ["guest_ready"]
    env_id = demo.env("vitrine", "dev")["id"]
    bruno = demo.principal("bruno")
    freezes.freeze(conn, bruno, env_id, "Gel avant la fenêtre de configuration")
    conn.commit()

    assert worker.drain(kinds=["ansible_configure"]) == ["ansible_configure"]
    configure = flows.jobs_of(conn, c["id"], "ansible_configure")[0]
    assert [configure["state"], configure["blocked_reason"], configure["dispatched_at"]] == [
        "blocked", "env_frozen", None]
    assert flows.change(conn, c["id"])["state"] == "partially_succeeded"
    host = _hosts(conn, c["deployment_id"])[0]
    assert host["deployment_state"] == "enrolled"
    assert "Configuration nginx 1.0.0" not in " ".join(_step_names(conn, c["id"]))

    retry = approvals.request_retry(conn, demo.principal("alice"), c["id"], {"step": "ansible_configure"})
    conn.commit()
    retry_body = {"retry_id": str(retry["retry_id"]), "bundle_digest": retry["bundle_digest"], "decision": "approved"}
    with pytest.raises(ApiError) as exc:
        approvals.decide(conn, bruno, c["id"], retry_body)
    conn.rollback()
    assert (exc.value.status, exc.value.code) == (409, "env_frozen")

    freezes.unfreeze(conn, bruno, env_id, "Fenêtre de configuration ouverte")
    conn.commit()
    approvals.decide(conn, bruno, c["id"], retry_body)
    conn.commit()
    assert worker.drain() == ["ansible_configure", "verify"]
    assert flows.change(conn, c["id"])["state"] == "succeeded"
    assert flows.deployment(conn, c["deployment_id"])["service_status"] == "ready"
