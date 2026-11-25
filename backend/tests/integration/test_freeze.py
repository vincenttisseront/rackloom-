"""F28 : gel d'urgence par environnement.

Job d'écriture en file puis gel : bloqué `env_frozen` au démarrage effectif (R080) ; gel entre
l'admission et l'envoi : bloqué à l'envoi ; gel concurrent de l'admission : sérialisé par le
verrou de l'environnement ; job déjà envoyé : poursuivi et suivi ; lectures admises pendant le
gel ; approbation suspendue ; droits (404 hors périmètre, 403 sans le rôle), audit et événements.
Niveau de preuve : testé localement (PostgreSQL éphémère, Terraform CLI réel, vCenter fictif).
"""

from __future__ import annotations

import threading
import time

import psycopg
import pytest
from psycopg.rows import dict_row

import flows
from rackloom import admission, audit
from rackloom.db import transaction
from rackloom.errors import ApiError
from rackloom.executions import queue
from rackloom.projects import freezes
from rackloom.worker import handlers

API = "/api/v1"
REASON = "Incident réseau en cours sur le site"


def _wait(predicate, timeout: float = 60, step: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("délai dépassé")


def _approved(conn, demo, worker, **kw):
    c = flows.create_and_submit(conn, demo, **kw)
    assert worker.drain(kinds=["tf_plan"]) == ["tf_plan"]
    plan = flows.latest_plan(conn, c["id"])
    res = flows.approve(conn, demo, c["id"], plan)
    return c, plan, res["job_id"]


def _freeze(conn, demo, who: str = "bruno", env: tuple[str, str] = ("vitrine", "dev"), reason: str = REASON):
    res = freezes.freeze(conn, demo.principal(who), demo.env(*env)["id"], reason)
    conn.commit()
    return res


def _decisions(conn, job_id):
    conn.commit()
    return conn.execute("SELECT stage, outcome, rules FROM admission_decisions WHERE job_id = %s ORDER BY id",
                        (job_id,)).fetchall()


def _event_types(conn, change_id):
    conn.commit()
    return [e["type"] for e in conn.execute("SELECT type FROM events WHERE correlation_id = %s ORDER BY id",
                                            (change_id,)).fetchall()]


def _vm_files(data):
    return sorted(p.name for p in (data / "demo-infra").glob("*/*.json"))


@pytest.mark.terraform
def test_f28_job_en_file_puis_gel_bloque_au_demarrage(conn, demo, worker, worker_env, api):
    c, plan, job_id = _approved(conn, demo, worker)
    assert flows.job(conn, job_id)["state"] == "queued"
    res = _freeze(conn, demo)
    assert res["write_jobs"] == {"queued": 1, "dispatched": 0}

    assert worker.drain(kinds=["tf_apply"]) == ["tf_apply"]
    job = flows.job(conn, job_id)
    assert [job["state"], job["blocked_reason"], job["dispatched_at"]] == ["blocked", "env_frozen", None]
    assert flows.change(conn, c["id"])["state"] == "blocked"
    decisions = _decisions(conn, job_id)
    assert [(d["stage"], d["outcome"]) for d in decisions] == [("start", "refused")]
    assert decisions[0]["rules"][-1]["id"] == "R080" and decisions[0]["rules"][-1]["code"] == "env_frozen"
    assert _vm_files(worker_env) == []
    assert not (worker_env / "workspaces" / str(job_id)).exists()
    types = _event_types(conn, c["id"])
    assert types.index("environment.frozen") < types.index("job.blocked")
    blocked = conn.execute("SELECT data FROM events WHERE correlation_id = %s AND type = 'job.blocked'",
                           (c["id"],)).fetchone()["data"]
    assert blocked["code"] == "env_frozen" and blocked["stage"] == "start" and REASON in blocked["message"]

    view = api("alice").get(f"{API}/changes/{c['id']}").json()
    assert view["state"] == "blocked" and view["environment_freeze"]["reason"] == REASON
    assert view["environment_freeze"]["frozen_by"] == "Bruno (approbateur)"
    assert "R080" in view["reason"] or "gelé" in view["reason"]


@pytest.mark.terraform
def test_f28_gel_entre_admission_et_envoi(conn, demo, worker, worker_env, monkeypatch):
    c, plan, job_id = _approved(conn, demo, worker)
    env_id = demo.env("vitrine", "dev")["id"]
    original = handlers._prepare_tf

    def prepare_then_freeze(*args, **kwargs):
        work = original(*args, **kwargs)
        with transaction() as tx:
            freezes.freeze(tx, demo.principal("bruno"), env_id, "Gel pendant la préparation du runner")
        return work

    monkeypatch.setattr(handlers, "_prepare_tf", prepare_then_freeze)
    assert worker.drain(kinds=["tf_apply"]) == ["tf_apply"]
    job = flows.job(conn, job_id)
    assert [job["state"], job["blocked_reason"], job["dispatched_at"]] == ["blocked", "env_frozen", None]
    assert [(d["stage"], d["outcome"]) for d in _decisions(conn, job_id)] == [("start", "admitted"),
                                                                              ("dispatch", "refused")]
    assert flows.change(conn, c["id"])["state"] == "blocked"
    assert not (worker_env / "workspaces" / str(job_id)).exists()
    assert _vm_files(worker_env) == []
    stack = conn.execute("SELECT lock_job_id FROM stacks WHERE id = %s", (job["stack_id"],)).fetchone()
    assert stack["lock_job_id"] is None


@pytest.mark.terraform
def test_f28_gel_concurrent_serialise_avec_l_admission(conn, demo, worker, worker_env, db_url):
    c, plan, job_id = _approved(conn, demo, worker)
    env_id = demo.env("vitrine", "dev")["id"]
    with psycopg.connect(db_url, row_factory=dict_row) as locker:
        freezes.freeze(locker, demo.principal("bruno"), env_id, "Gel en cours de validation")
        t = threading.Thread(target=worker.drain, kwargs={"kinds": ["tf_apply"]})
        t.start()
        time.sleep(2)
        pending = flows.job(conn, job_id)
        assert pending["state"] == "leased" and pending["dispatched_at"] is None
        locker.commit()
    t.join(timeout=120)
    job = flows.job(conn, job_id)
    assert [job["state"], job["blocked_reason"], job["dispatched_at"]] == ["blocked", "env_frozen", None]
    assert _vm_files(worker_env) == []


@pytest.mark.terraform
def test_f28_job_deja_envoye_poursuivi_et_suivi(conn, demo, worker, worker_env, api):
    conn.execute("INSERT INTO platform_settings (key, value) VALUES ('lab_delay_seconds', '6')")
    conn.commit()
    c, plan, job_id = _approved(conn, demo, worker)
    t = threading.Thread(target=worker.drain, kwargs={"kinds": ["tf_apply"]})
    t.start()
    _wait(lambda: flows.job(conn, job_id)["dispatched_at"] is not None)
    res = _freeze(conn, demo)
    assert res["write_jobs"] == {"queued": 0, "dispatched": 1}
    frozen = api("bruno").get(f"{API}/projects/vitrine/environments").json()
    dev = next(e for e in frozen if e["slug"] == "dev")
    assert dev["frozen"] and dev["write_jobs"] == {"queued": 0, "dispatched": 1}
    t.join(timeout=180)

    job = flows.job(conn, job_id)
    assert job["state"] == "succeeded", job
    assert _vm_files(worker_env) == ["vit-dev-web-01.json"]
    assert flows.change(conn, c["id"])["state"] == "executing"
    # La suite de la chaîne est en lecture : admise pendant le gel, l'état réel reste affiché.
    assert worker.drain() == ["guest_ready", "verify"]
    for kind in ("guest_ready", "verify"):
        decision = _decisions(conn, flows.jobs_of(conn, c["id"], kind)[0]["id"])
        assert [(d["stage"], d["outcome"]) for d in decision] == [("start", "admitted")]
        assert "R080" not in [r["id"] for r in decision[0]["rules"]]
    assert flows.change(conn, c["id"])["state"] == "partially_succeeded"
    view = api("alice").get(f"{API}/changes/{c['id']}").json()
    assert view["deployment"]["service_status"] == "created_not_ready"
    assert view["environment_freeze"]["reason"] == REASON
    assert [j["state"] for j in api("alice").get(f"{API}/changes/{c['id']}/jobs").json()] == [
        "succeeded", "succeeded", "succeeded", "succeeded"]


@pytest.mark.terraform
def test_f28_lecture_admise_et_approbation_suspendue(conn, demo, worker, api):
    _freeze(conn, demo)
    c = flows.create_and_submit(conn, demo)
    assert worker.drain(kinds=["tf_plan"]) == ["tf_plan"]
    assert flows.change(conn, c["id"])["state"] == "plan_ready"
    plan_decision = _decisions(conn, c["job_id"])
    assert plan_decision[0]["outcome"] == "admitted"
    assert "R080" not in [r["id"] for r in plan_decision[0]["rules"]]

    plan = flows.latest_plan(conn, c["id"])
    with pytest.raises(ApiError) as exc:
        flows.approve(conn, demo, c["id"], plan)
    conn.rollback()
    assert (exc.value.status, exc.value.code) == (409, "env_frozen")
    bruno = api("bruno")
    view = bruno.get(f"{API}/changes/{c['id']}").json()
    assert [a["action"] for a in view["allowed_actions"] if a["action"] in ("approve", "reject")] == ["reject"]
    r = bruno.post(f"{API}/changes/{c['id']}/approvals",
                   json={"plan_id": str(plan["id"]), "bundle_digest": plan["bundle_digest"], "decision": "approved"})
    assert r.status_code == 409 and r.json()["code"] == "env_frozen", r.text
    assert conn.execute("SELECT count(*) AS n FROM approvals WHERE change_id = %s", (c["id"],)).fetchone()["n"] == 0

    # Les jobs de lecture (inventaire, collecte, vérification, réconciliation) ne lisent pas le gel.
    change = flows.change(conn, c["id"])
    env = demo.env("vitrine", "dev")
    for kind in ("inventory_refresh", "tf_state_read", "guest_ready", "verify", "reconcile"):
        job = queue.enqueue(conn, kind=kind, change=change, idempotency_key=f"f28-{kind}:{c['id']}")
        decision = admission.evaluate(admission.Context(conn=conn, job=job, change=change, environment=env,
                                                        execution=None, approval=None, plan=None))
        assert decision["outcome"] == "admitted", (kind, decision)
        assert "R080" not in [r["id"] for r in decision["rules"]]
    conn.rollback()
    admin = api("admin")
    connector = conn.execute("SELECT id FROM connectors WHERE slug = 'vsphere-fake'").fetchone()
    assert admin.post(f"{API}/connectors/{connector['id']}/inventory-refresh").status_code == 200

    freezes.unfreeze(conn, demo.principal("bruno"), env["id"], "Incident clos")
    conn.commit()
    res = flows.approve(conn, demo, c["id"], plan)
    assert res["decision"] == "approved"


def test_f28_droits_audit_evenements(conn, demo, api):
    env = demo.env("vitrine", "dev")
    url = f"{API}/environments/{env['id']}"
    alice, bruno, chloe, david, admin = (api(n) for n in ("alice", "bruno", "chloe", "david", "admin"))
    body = {"reason": REASON}

    assert chloe.post(f"{url}/freeze", json=body).status_code == 404
    assert chloe.get(url).status_code == 404
    for who in (alice, david):
        r = who.post(f"{url}/freeze", json=body)
        assert r.status_code == 403 and r.json()["code"] == "forbidden", r.text
    r = bruno.post(f"{url}/freeze", json={"reason": "no"})
    assert r.status_code == 422 and r.json()["field"] == "/reason"
    r = bruno.post(f"{url}/freeze", json={"reason": REASON, "until": "2030-01-01"})
    assert r.status_code == 422 and r.json()["code"] == "unknown_field"
    no_csrf = api("bruno")
    del no_csrf.headers["X-CSRF-Token"]
    assert no_csrf.post(f"{url}/freeze", json=body).status_code == 403

    r = bruno.post(f"{url}/freeze", json={"reason": "Incident, password=hunter2-demo"},
                   headers={"Idempotency-Key": "f28-gel-1"})
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["frozen"] and view["freeze"]["frozen_by"] == "Bruno (approbateur)"
    assert "hunter2" not in r.text and "[secret masqué]" in view["freeze"]["reason"]
    assert [a["action"] for a in view["allowed_actions"]] == ["unfreeze", "manage_calendar"]
    replay = bruno.post(f"{url}/freeze", json={"reason": "Incident, password=hunter2-demo"},
                        headers={"Idempotency-Key": "f28-gel-1"})
    assert replay.status_code == 200 and replay.json()["freeze"]["id"] == view["freeze"]["id"]
    again = bruno.post(f"{url}/freeze", json=body)
    assert again.status_code == 409, again.text

    envs = {e["slug"]: e for e in alice.get(f"{API}/projects/vitrine/environments").json()}
    assert envs["dev"]["frozen"] and not envs["prod"]["frozen"]
    assert envs["dev"]["allowed_actions"] == []
    assert alice.post(f"{url}/unfreeze", json=body).status_code == 403
    assert not any(e["frozen"] for e in chloe.get(f"{API}/projects/compta/environments").json())
    # D7 : l'administrateur ne lit pas le projet, mais gèle et dégèle d'urgence depuis l'administration.
    assert admin.get(f"{API}/projects/vitrine/environments").status_code == 404
    listing = {f"{e['project']['slug']}/{e['slug']}": e for e in admin.get(f"{API}/admin/environments").json()}
    assert set(listing) == {"vitrine/dev", "vitrine/prod", "compta/dev"}
    assert listing["vitrine/dev"]["frozen"]
    assert [a["action"] for a in listing["vitrine/dev"]["allowed_actions"]] == ["unfreeze", "manage_calendar"]
    audit_view = david.get(f"{API}/admin/environments")
    assert audit_view.status_code == 200 and all(e["allowed_actions"] == [] for e in audit_view.json())
    assert alice.get(f"{API}/admin/environments").status_code == 403

    r = admin.post(f"{url}/unfreeze", json={"reason": "Incident clos, retour à la normale"})
    assert r.status_code == 200 and not r.json()["frozen"], r.text
    assert admin.post(f"{url}/unfreeze", json=body).status_code == 409
    history = alice.get(url).json()["history"]
    assert len(history) == 1 and history[0]["lifted_by"] == "Admin (démo)"
    assert history[0]["lift_reason"] == "Incident clos, retour à la normale"

    conn.commit()
    rows = conn.execute("SELECT action, actor_id, project_id, targets, policy_decision FROM audit_log "
                        "WHERE action LIKE 'environment.%' ORDER BY id").fetchall()
    assert [r["action"] for r in rows] == ["environment.freeze", "environment.unfreeze"]
    assert [r["actor_id"] for r in rows] == [str(demo.user("bruno")["id"]), str(demo.user("admin")["id"])]
    assert all(r["project_id"] == env["project_id"] and r["targets"]["environment_id"] == str(env["id"]) for r in rows)
    assert "hunter2" not in str(rows[0]["policy_decision"])
    assert audit.verify(conn)["ok"]
    events = conn.execute("SELECT type, project_id, correlation_id FROM events WHERE type LIKE 'environment.%' "
                          "ORDER BY id").fetchall()
    assert [(e["type"], e["project_id"], e["correlation_id"]) for e in events] == [
        ("environment.frozen", env["project_id"], None), ("environment.unfrozen", env["project_id"], None)]

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE environment_freezes SET reason = 'motif réécrit'")
    conn.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("DELETE FROM environment_freezes")
    conn.rollback()
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO environment_freezes (id, environment_id, project_id, reason, frozen_by) "
                     "SELECT gen_random_uuid(), %s, %s, 'gel 1', %s UNION ALL "
                     "SELECT gen_random_uuid(), %s, %s, 'gel 2', %s",
                     (env["id"], env["project_id"], demo.user("bruno")["id"]) * 2)
    conn.rollback()


def test_f28_gel_d_un_environnement_n_affecte_pas_les_autres(conn, demo):
    _freeze(conn, demo, env=("vitrine", "prod"))
    assert freezes.active(conn, demo.env("vitrine", "prod")["id"]) is not None
    assert freezes.active(conn, demo.env("vitrine", "dev")["id"]) is None
    assert freezes.active(conn, demo.env("compta", "dev")["id"]) is None
    with pytest.raises(ApiError) as exc:
        freezes.freeze(conn, demo.principal("chloe"), demo.env("vitrine", "dev")["id"], REASON)
    assert exc.value.status == 404
    conn.rollback()
    with pytest.raises(ApiError) as exc:
        freezes.freeze(conn, demo.principal("chloe"), demo.env("compta", "dev")["id"], REASON)
    assert exc.value.status == 403
    conn.rollback()
