"""Plan Terraform réel sur ressources locales jetables, approbation liée à l'empreinte, admission.

Couvre T01, T03, T04, T05, T07, T08, T26, T35, T40 et l'acceptation de E5 (plan chiffré, 2 créations).
Niveau de preuve : testé localement (Terraform CLI réel, launcher en mode processus).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

import flows
from rackloom import approvals, artifacts, registry
from rackloom.changes import service
from rackloom.errors import ApiError

pytestmark = pytest.mark.terraform


def _plan_ready(conn, demo, worker, **kw):
    c = flows.create_and_submit(conn, demo, **kw)
    assert worker.drain(kinds=["tf_plan"]) == ["tf_plan"]
    assert flows.change(conn, c["id"])["state"] == "plan_ready"
    return c, flows.latest_plan(conn, c["id"])


def test_plan_reel_deux_creations_chiffre(conn, demo, worker):
    c, plan = _plan_ready(conn, demo, worker, quantity=2)
    assert plan["status"] == "ready"
    assert plan["counts"]["create"] == 2 and plan["counts"]["delete"] == 0
    assert plan["summary"]["terraform_version"]
    assert {vm["name"] for vm in plan["summary"]["vms"]} == {"vit-dev-web-01", "vit-dev-web-02"}
    assert plan["revision_no"] == c["revision_no"]
    assert plan["bundle"]["request"]["resolution_digest"].startswith("sha256:")
    assert plan["bundle"]["template"]["version"] == "1.0.0"
    assert plan["bundle"]["environment"]["policy_version"] == 1
    # Ni plan.bin ni plan.json en base ; le fichier chiffré n'est pas une archive lisible.
    art = conn.execute("SELECT * FROM artifacts WHERE id = %s", (plan["artifact_id"],)).fetchone()
    assert art["key_class"] == "A"
    path = artifacts.path_of(art)
    assert not zipfile.is_zipfile(path)
    assert zipfile.is_zipfile(__import__("io").BytesIO(artifacts.read(art)))
    for table in ("events", "audit_log", "jobs", "plans"):
        dump = conn.execute(f"SELECT string_agg(t::text, '') AS s FROM {table} t").fetchone()["s"] or ""
        assert "resource_changes" not in dump and "PK\x03\x04" not in dump
    # Aucun espace de travail résiduel après collecte.
    assert not any((Path(worker.settings.workspaces_dir)).glob("*/out/plan.bin"))


def test_application_cree_exactement_les_vm(conn, demo, worker, worker_env):
    c, plan = _plan_ready(conn, demo, worker, quantity=2)
    res = flows.approve(conn, demo, c["id"], plan)
    assert worker.drain(kinds=["tf_apply"]) == ["tf_apply"]
    job = flows.job(conn, res["job_id"])
    assert job["state"] == "succeeded", job
    assert job["dispatched_at"] is not None
    files = sorted(p.name for p in (worker_env / "demo-infra" / str(c["deployment_id"])).glob("*.json"))
    assert files == ["vit-dev-web-01.json", "vit-dev-web-02.json"]
    dep = flows.deployment(conn, c["deployment_id"])
    assert dep["status"] == "active"
    assert [i["name"] for i in dep["instances"]] == ["vit-dev-web-01", "vit-dev-web-02"]
    assert dep["service_status"] == "unknown"
    assert flows.change(conn, c["id"])["state"] == "executing"
    assert [j["kind"] for j in flows.jobs_of(conn, c["id"])][-1] == "guest_ready"
    decisions = conn.execute("SELECT outcome FROM admission_decisions WHERE job_id = %s", (job["id"],)).fetchall()
    assert [d["outcome"] for d in decisions] == ["admitted"]


def test_t01_plan_altere_bloque(conn, demo, worker, worker_env):
    c, plan = _plan_ready(conn, demo, worker)
    flows.approve(conn, demo, c["id"], plan)
    art = conn.execute("SELECT * FROM artifacts WHERE id = %s", (plan["artifact_id"],)).fetchone()
    path = artifacts.path_of(art)
    data = bytearray(path.read_bytes())
    data[-1] ^= 0x01
    path.write_bytes(bytes(data))
    worker.drain(kinds=["tf_apply"])
    job = flows.jobs_of(conn, c["id"], "tf_apply")[0]
    assert job["state"] in ("blocked", "failed")
    assert job["dispatched_at"] is None
    assert not list((worker_env / "demo-infra").glob("*/*.json"))
    assert flows.change(conn, c["id"])["state"] in ("blocked", "plan_failed", "approved")


def test_t01_plan_remplace_bloque_par_empreinte(conn, demo, worker, worker_env):
    """Plan binaire substitué par un autre plan valide : SHA-256 différent, admission refusée."""
    c, plan = _plan_ready(conn, demo, worker)
    flows.approve(conn, demo, c["id"], plan)
    other, other_plan = _plan_ready(conn, demo, worker, size_tier="standard")
    conn.execute("UPDATE plans SET artifact_id = %s WHERE id = %s", (other_plan["artifact_id"], plan["id"]))
    conn.commit()
    worker.drain(kinds=["tf_apply"])
    job = flows.jobs_of(conn, c["id"], "tf_apply")[0]
    assert job["state"] == "blocked" and job["blocked_reason"] == "plan_altered"
    assert not list((worker_env / "demo-infra").glob("*/*.json"))
    assert flows.change(conn, c["id"])["state"] == "blocked"


def test_t03_nouvelle_version_revoque_et_bloque(conn, demo, worker, worker_env):
    c, plan = _plan_ready(conn, demo, worker)
    res = flows.approve(conn, demo, c["id"], plan)
    service.add_version(conn, demo.principal("alice"), c["id"],
                        {"need": flows.need(size_tier="standard"), "base_version_no": c["version_no"]})
    conn.commit()
    approval = conn.execute("SELECT * FROM approvals WHERE id = %s", (res["approval_id"],)).fetchone()
    assert approval["revoked_reason"] == "new_version"
    worker.drain(kinds=["tf_apply"])
    job = flows.job(conn, res["job_id"])
    assert job["state"] == "blocked"
    assert job["blocked_reason"] in ("approval_revoked", "bundle_mismatch")
    assert not list((worker_env / "demo-infra").glob("*/*.json"))


def test_t04_plan_expire(conn, demo, worker):
    conn.execute("UPDATE environments SET plan_ttl_seconds = 2")
    conn.commit()
    c, plan = _plan_ready(conn, demo, worker)
    # Approuvable pendant 2 s, mais la marge d'admission (60 s) le refuse au démarrage.
    res = flows.approve(conn, demo, c["id"], plan)
    worker.drain(kinds=["tf_apply"])
    job = flows.job(conn, res["job_id"])
    assert job["state"] == "blocked" and job["blocked_reason"] == "plan_expired"
    assert flows.change(conn, c["id"])["state"] == "plan_expired"
    # Approuver un plan expiré : 409.
    c2, plan2 = _plan_ready(conn, demo, worker)
    conn.execute("UPDATE plans SET valid_until = now() - interval '1 second' WHERE id = %s", (plan2["id"],))
    conn.commit()
    with pytest.raises(ApiError) as exc:
        flows.approve(conn, demo, c2["id"], plan2)
    assert exc.value.status == 409 and exc.value.code == "plan_expired"
    conn.commit()
    assert flows.change(conn, c2["id"])["state"] == "plan_expired"


def test_t05_approbation_rejouee(conn, demo, worker):
    c, plan = _plan_ready(conn, demo, worker)
    with pytest.raises(ApiError) as exc:
        flows.approve(conn, demo, c["id"], plan, digest="0" * 64)
    assert exc.value.code == "stale_digest"
    conn.rollback()
    flows.approve(conn, demo, c["id"], plan)
    with pytest.raises(ApiError) as exc:
        flows.approve(conn, demo, c["id"], plan)
    assert exc.value.status == 409
    conn.rollback()
    assert len(flows.jobs_of(conn, c["id"], "tf_apply")) == 1
    # Seconde exécution avec la même approbation : contrainte unique.
    import psycopg

    approval = conn.execute("SELECT * FROM approvals WHERE change_id = %s", (c["id"],)).fetchone()
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO executions (id, change_id, approval_id, bundle_digest, state) "
                     "VALUES (gen_random_uuid(), %s, %s, 'x', 'pending')", (c["id"], approval["id"]))
    conn.rollback()


def test_t07_role_revoque_avant_demarrage(conn, demo, worker, worker_env):
    c, plan = _plan_ready(conn, demo, worker)
    res = flows.approve(conn, demo, c["id"], plan)
    conn.execute("UPDATE role_bindings SET revoked_at = now() WHERE user_id = %s AND role = 'approver'",
                 (demo.user("bruno")["id"],))
    conn.commit()
    worker.drain(kinds=["tf_apply"])
    job = flows.job(conn, res["job_id"])
    assert job["state"] == "blocked" and job["blocked_reason"] == "approver_revoked"
    # Opératrice révoquée : la planification suivante est refusée aussi.
    c2 = flows.create_and_submit(conn, demo)
    conn.execute("UPDATE role_bindings SET revoked_at = now() WHERE user_id = %s AND role = 'operator'",
                 (demo.user("alice")["id"],))
    conn.commit()
    worker.drain(kinds=["tf_plan"])
    assert flows.job(conn, c2["job_id"])["blocked_reason"] == "requester_revoked"
    assert not list((worker_env / "demo-infra").glob("*/*.json"))


def test_t08_separation_des_roles(conn, demo, worker):
    c, plan = _plan_ready(conn, demo, worker)
    conn.execute("INSERT INTO role_bindings (id, user_id, role, project_id) VALUES (gen_random_uuid(), %s, "
                 "'approver', %s)", (demo.user("alice")["id"], demo.project("vitrine")["id"]))
    conn.commit()
    with pytest.raises(ApiError) as exc:
        flows.approve(conn, demo, c["id"], plan, who="alice")
    assert exc.value.status == 403 and exc.value.code == "self_approval"
    conn.rollback()
    with pytest.raises(ApiError) as exc:
        flows.approve(conn, demo, c["id"], plan, who="chloe")
    assert exc.value.status == 404
    conn.rollback()


def _deployed(conn, demo, worker, **kw):
    """Déploiement actif (application réelle), sans les étapes invité ; changement clos en succès."""
    c, plan = _plan_ready(conn, demo, worker, **kw)
    flows.approve(conn, demo, c["id"], plan)
    worker.drain(kinds=["tf_apply"])
    conn.execute("UPDATE jobs SET state = 'cancelled' WHERE change_id = %s AND state = 'queued'", (c["id"],))
    conn.execute("UPDATE changes SET state = 'succeeded' WHERE id = %s", (c["id"],))
    conn.commit()
    return c


def _modify(conn, demo, worker, deployment_id, **need_kw):
    alice = demo.principal("alice")
    m = service.create_modify_change(conn, alice, deployment_id)
    res = service.add_version(conn, alice, m["id"], {"need": flows.need(**need_kw), "base_version_no": 0})
    assert res["revision"]["outcome"] == "resolved", res["revision"]
    service.submit(conn, alice, m["id"], res["revision_no"])
    conn.commit()
    worker.drain(kinds=["tf_plan"])
    return m, res, flows.latest_plan(conn, m["id"])


def test_t26_changement_de_niveau_ou_reduction_bloques(conn, demo, worker, worker_env):
    c = _deployed(conn, demo, worker, quantity=2)
    # « Modifier ce déploiement » part du besoin en vigueur, même avant la première version.
    from rackloom.api.views import change_view

    alice = demo.principal("alice")
    draft = service.create_modify_change(conn, alice, c["deployment_id"])
    start = change_view(conn, alice, draft)["need"]
    assert start["service"]["template"] == "ubuntu-web" and start["inputs"]["quantity"] == 2
    service.cancel(conn, alice, draft["id"])
    conn.commit()
    m, res, plan = _modify(conn, demo, worker, c["deployment_id"], size_tier="standard")
    assert [i["existing"] for i in res["revision"]["resolved"]["instances"]] == [True, True]
    names = [i["name"] for i in res["revision"]["resolved"]["instances"]]
    # Risque annoncé dès l'étape 2, avant tout plan.
    assert res["summary"]["replaces"] == names and res["summary"]["removes"] == []
    assert plan["status"] == "blocked_destructive"
    assert plan["summary"]["vm_counts"]["replace"] == 2
    with pytest.raises(ApiError) as exc:
        flows.approve(conn, demo, m["id"], plan)
    assert exc.value.code == "destructive_plan"
    conn.rollback()
    service.cancel(conn, demo.principal("alice"), m["id"])
    conn.commit()
    m2, res2, plan2 = _modify(conn, demo, worker, c["deployment_id"], quantity=1)
    assert res2["summary"]["removes"] == names[1:] and res2["summary"]["replaces"] == []
    assert plan2["status"] == "blocked_destructive"
    assert plan2["summary"]["vm_counts"]["delete"] == 1
    with pytest.raises(ApiError) as exc:
        flows.approve(conn, demo, m2["id"], plan2)
    assert exc.value.code == "destructive_plan"
    conn.rollback()
    assert len(list((worker_env / "demo-infra" / str(c["deployment_id"])).glob("*.json"))) == 2


def test_t35_d5_version_epinglee_puis_retrait(conn, demo, worker, worker_env):
    c, plan = _plan_ready(conn, demo, worker)
    res = flows.approve(conn, demo, c["id"], plan)
    pub = demo.principal("pub")
    registry.publish_action(conn, pub, "ubuntu-web", "1.1.0", "publish", "Nouvelle version")
    conn.commit()
    worker.drain(kinds=["tf_apply"])
    job = flows.job(conn, res["job_id"])
    assert job["state"] == "succeeded", job
    assert flows.latest_plan(conn, c["id"])["bundle_digest"] == plan["bundle_digest"]
    dep = flows.deployment(conn, c["deployment_id"])
    tpl = registry.template_by_id(conn, dep["template_version_id"])
    assert tpl["version"] == "1.0.0"
    vm = next((worker_env / "demo-infra" / str(c["deployment_id"])).glob("*.json"))
    assert '"memory_mb":2048' in vm.read_text()
    # Nouvelle demande : dernière version publiée.
    c2, plan2 = _plan_ready(conn, demo, worker)
    assert plan2["bundle"]["template"]["version"] == "1.1.0"
    # Plan 1.0.0 approuvé, puis 1.0.0 retirée avant le démarrage : bloqué par la règle 55, plan intact.
    c3, plan3 = _plan_ready(conn, demo, worker)
    registry.publish_action(conn, pub, "ubuntu-web", "1.1.0", "withdraw", "Retrait de test")
    conn.commit()
    c4, plan4 = _plan_ready(conn, demo, worker)
    assert plan4["bundle"]["template"]["version"] == "1.0.0"
    res4 = flows.approve(conn, demo, c4["id"], plan4)
    registry.publish_action(conn, pub, "ubuntu-web", "1.0.0", "withdraw", "Retrait de test")
    conn.commit()
    worker.drain(kinds=["tf_apply"])
    job4 = flows.job(conn, res4["job_id"])
    assert job4["state"] == "blocked" and job4["blocked_reason"] == "resolution_no_longer_admissible"
    assert job4["dispatched_at"] is None
    after = flows.latest_plan(conn, c4["id"])
    assert after["bundle_digest"] == plan4["bundle_digest"] and after["status"] == "ready"
    assert flows.change(conn, c4["id"])["current_revision_no"] == c4["revision_no"]
    assert not list((worker_env / "demo-infra" / str(c4["deployment_id"])).glob("*.json"))
    decision = conn.execute("SELECT rules FROM admission_decisions WHERE job_id = %s", (job4["id"],)).fetchone()
    assert decision["rules"][-1]["id"] == "R055"


def test_t40_octroi_retire_entre_approbation_et_demarrage(conn, demo, worker, worker_env):
    c, plan = _plan_ready(conn, demo, worker)
    res = flows.approve(conn, demo, c["id"], plan)
    conn.execute("UPDATE resource_grants SET revoked_at = now() WHERE stable_id = 'network-101'")
    conn.commit()
    worker.drain(kinds=["tf_apply"])
    job = flows.job(conn, res["job_id"])
    assert job["state"] == "blocked" and job["blocked_reason"] == "resolution_no_longer_admissible"
    assert flows.change(conn, c["id"])["current_revision_no"] == c["revision_no"]
    assert conn.execute("SELECT count(*) AS n FROM request_revisions WHERE change_id = %s",
                        (c["id"],)).fetchone()["n"] == 1
    assert not list((worker_env / "demo-infra" / str(c["deployment_id"])).glob("*.json"))


def test_refus_avec_motif(conn, demo, worker):
    c, plan = _plan_ready(conn, demo, worker)
    with pytest.raises(ApiError):
        approvals.decide(conn, demo.principal("bruno"), c["id"], {
            "plan_id": str(plan["id"]), "bundle_digest": plan["bundle_digest"], "decision": "rejected"})
    conn.rollback()
    approvals.decide(conn, demo.principal("bruno"), c["id"], {
        "plan_id": str(plan["id"]), "bundle_digest": plan["bundle_digest"], "decision": "rejected",
        "comment": "Taille inadaptée"})
    conn.commit()
    assert flows.change(conn, c["id"])["state"] == "rejected"
    assert not flows.jobs_of(conn, c["id"], "tf_apply")
