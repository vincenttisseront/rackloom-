"""Plan Terraform réel sur ressources locales jetables, approbation liée à l'empreinte, admission.

Couvre T01, T03, T04, T05, T07, T08, T26 et l'acceptation de E5 (plan chiffré, 2 créations).
Niveau de preuve : testé localement (Terraform CLI réel, launcher en mode processus).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

import flows
from rackloom import approvals, artifacts
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
    assert {vm["name"] for vm in plan["summary"]["vms"]} == {"vitrine-lab-web-01", "vitrine-lab-web-02"}
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
    files = sorted(p.name for p in (worker_env / "demo-infra").glob("*/*.json"))
    assert files == ["vitrine-lab-web-01.json", "vitrine-lab-web-02.json"]
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
    other, other_plan = _plan_ready(conn, demo, worker, size="light")
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
                        {"need": flows.need(size="high"), "base_version_no": c["version_no"]})
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


def test_t26_changement_de_taille_bloque(conn, demo, worker, worker_env):
    c, plan = _plan_ready(conn, demo, worker)
    flows.approve(conn, demo, c["id"], plan)
    worker.drain(kinds=["tf_apply"])
    conn.execute("UPDATE changes SET state = 'succeeded' WHERE id = %s", (c["id"],))
    conn.execute("UPDATE jobs SET state = 'cancelled' WHERE change_id = %s AND state = 'queued'", (c["id"],))
    conn.commit()
    alice = demo.principal("alice")
    res = service.add_version(conn, alice, c["id"], {"need": flows.need(size="high"), "base_version_no": 1})
    service.submit(conn, alice, c["id"], res["version_no"])
    conn.commit()
    worker.drain(kinds=["tf_plan"])
    plan2 = flows.latest_plan(conn, c["id"])
    assert plan2["status"] == "blocked_destructive"
    assert plan2["summary"]["vm_counts"]["replace"] == 1
    with pytest.raises(ApiError) as exc:
        flows.approve(conn, demo, c["id"], plan2)
    assert exc.value.code == "destructive_plan"
    conn.rollback()


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
