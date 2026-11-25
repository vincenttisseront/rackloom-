"""Résilience du worker : concurrence et époque (T15), redémarrage avant envoi (T14), worker tué
après envoi (T13), annulation pendant l'application (T23), state modifié après le plan (T02),
nouvelle planification sans changement (T27). Niveau de preuve : testé localement.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import flows
from rackloom.changes import service
from rackloom.db import transaction
from rackloom.executions import queue
from rackloom.worker.main import Worker

pytestmark = pytest.mark.terraform
ROOT = Path(__file__).resolve().parents[3]


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
    worker.drain(kinds=["tf_plan"])
    plan = flows.latest_plan(conn, c["id"])
    res = flows.approve(conn, demo, c["id"], plan)
    return c, plan, res["job_id"]


def _runs_for(data: Path, job_id) -> list[Path]:
    return [p for p in (data / "workspaces").glob("*/.run.json")
            if json.loads(p.read_text()).get("job_id") == str(job_id)]


def test_t15_deux_workers_chaque_job_une_fois(conn, demo, worker):
    changes = [flows.create_and_submit(conn, demo) for _ in range(4)]
    w2 = Worker(worker_id="test-worker-2")
    w2.register()
    threads = [threading.Thread(target=w.drain, kwargs={"kinds": ["tf_plan"]}) for w in (worker, w2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    for c in changes:
        job = flows.job(conn, c["job_id"])
        assert job["state"] == "succeeded" and job["attempts"] == 1
        decisions = conn.execute("SELECT count(*) AS n FROM admission_decisions WHERE job_id = %s",
                                 (job["id"],)).fetchone()
        assert decisions["n"] == 1
    owners = {flows.job(conn, c["job_id"])["lease_owner"] for c in changes}
    assert owners <= {"test-worker-1", "test-worker-2"}


def test_t15_bail_repris_interdit_toute_ecriture(conn, demo, worker, monkeypatch):
    c = flows.create_and_submit(conn, demo)
    with transaction() as tx:
        job_a = queue.claim(tx, "worker-a", 1)
    time.sleep(1.3)
    assert worker.reap() == 1
    with transaction() as tx:
        job_b = queue.claim(tx, "worker-b", 30)
    assert job_b["id"] == job_a["id"] and job_b["lease_epoch"] == job_a["lease_epoch"] + 1
    with pytest.raises(queue.LeaseLost), transaction() as tx:
        queue.fenced_update(tx, job_a, "state = 'succeeded'")
    with transaction() as tx:
        assert queue.fenced_update(tx, job_b, "heartbeat_at = now()")["lease_owner"] == "worker-b"
    assert flows.job(conn, c["job_id"])["state"] == "leased"


def test_t14_redemarrage_avant_envoi(conn, demo, worker, worker_env, monkeypatch):
    c, plan, job_id = _approved(conn, demo, worker)
    with transaction() as tx:
        claimed = queue.claim(tx, "worker-tue", 1, ["tf_apply"])
    assert claimed["id"] == job_id
    time.sleep(1.3)
    worker.reap()
    assert flows.job(conn, job_id)["state"] == "queued"
    worker.drain(kinds=["tf_apply"])
    job = flows.job(conn, job_id)
    assert job["state"] == "succeeded" and job["attempts"] == 2
    assert len(_runs_for(worker_env, job_id)) <= 1
    assert len(list((worker_env / "demo-infra").glob("*/*.json"))) == 1


def test_t13_worker_tue_apres_envoi(conn, demo, worker, worker_env):
    conn.execute("INSERT INTO platform_settings (key, value) VALUES ('lab_delay_seconds', '6')")
    conn.commit()
    c, plan, job_id = _approved(conn, demo, worker, quantity=2)
    env = {**os.environ, "RACKLOOM_JOB_LEASE_SECONDS": "3"}
    code = ("from rackloom.worker.main import Worker; w = Worker(worker_id='worker-condamne'); "
            "w.register(); w.run_once(['tf_apply'])")
    proc = subprocess.Popen([sys.executable, "-c", code], env=env)
    try:
        _wait(lambda: flows.job(conn, job_id)["dispatched_at"] is not None, 60)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(10)
    finally:
        if proc.poll() is None:
            proc.kill()
    time.sleep(3.5)
    assert worker.reap() >= 1
    assert flows.job(conn, job_id)["state"] == "uncertain"
    assert flows.change(conn, c["id"])["state"] == "uncertain"
    worker.drain(kinds=["reconcile"])
    job = flows.job(conn, job_id)
    assert job["state"] == "succeeded", job
    assert flows.change(conn, c["id"])["state"] == "executing"
    # Une seule exécution du runner, exactement 2 VM de labo, aucune relance automatique.
    assert len(_runs_for(worker_env, job_id)) <= 1
    assert sorted(p.name for p in (worker_env / "demo-infra").glob("*/*.json")) == [
        "vitrine-lab-web-01.json", "vitrine-lab-web-02.json"]
    assert len(flows.jobs_of(conn, c["id"], "tf_apply")) == 1
    types = [e["type"] for e in conn.execute("SELECT type FROM events WHERE correlation_id = %s ORDER BY id",
                                             (c["id"],)).fetchall()]
    assert "job.uncertain" in types


def test_t23_annulation_pendant_application(conn, demo, worker, worker_env):
    conn.execute("INSERT INTO platform_settings (key, value) VALUES ('lab_delay_seconds', '20')")
    conn.commit()
    c, plan, job_id = _approved(conn, demo, worker)
    t = threading.Thread(target=worker.drain, kwargs={"kinds": ["tf_apply"]})
    t.start()
    _wait(lambda: flows.job(conn, job_id)["dispatched_at"] is not None, 60)
    time.sleep(2)
    service.cancel_job(conn, demo.principal("alice"), job_id)
    conn.commit()
    t.join(timeout=120)
    job = flows.job(conn, job_id)
    state = flows.change(conn, c["id"])["state"]
    assert job["result"]["outcome"] in ("cancelled", "cancelled_partial")
    # Jamais « annulée » si des ressources existent ; aucune suppression.
    if job["result"]["vms"]:
        assert state == "failed"
    else:
        assert state == "cancelled"
    assert not list((worker_env / "demo-infra").glob("*/*.json"))


def test_t02_state_modifie_apres_le_plan(conn, demo, worker, worker_env, tmp_path):
    c, plan, job_id = _approved(conn, demo, worker)
    stack = conn.execute("SELECT slug FROM stacks WHERE change_id = %s", (c["id"],)).fetchone()["slug"]
    # Application hors Rackloom sur le même state (simule « une autre application »).
    tf = tmp_path / "intrus"
    shutil.copytree(ROOT / "catalog" / "terraform" / "lab-vm" / "1.0.0", tf)
    (tf / "zz_override.tf").write_text('terraform {\n  backend "local" {}\n}\n')
    state = worker_env / "tfstate" / stack
    labvms = worker_env / "demo-infra" / stack
    state.mkdir(parents=True, exist_ok=True)
    labvms.mkdir(parents=True, exist_ok=True)
    (tf / "x.auto.tfvars.json").write_text(json.dumps({"stack_id": stack, "labvms_dir": str(labvms), "vms": [
        {"name": "intrus", "size": "light", "cpu": 1, "memory_mb": 1024, "template_ref": "x", "network_ref": "y"}]}))
    env = {**os.environ, "TF_PLUGIN_CACHE_DIR": os.environ["RACKLOOM_PROCESS_TF_PLUGIN_CACHE_DIR"], "TF_INPUT": "0"}
    subprocess.run(["terraform", "init", "-input=false", "-lockfile=readonly",
                    f"-backend-config=path={state / 'terraform.tfstate'}"], cwd=tf, env=env, check=True,
                   capture_output=True)
    subprocess.run(["terraform", "apply", "-input=false", "-auto-approve"], cwd=tf, env=env, check=True,
                   capture_output=True)
    worker.drain(kinds=["tf_apply"])
    assert flows.change(conn, c["id"])["state"] == "plan_expired"
    assert flows.job(conn, job_id)["result"]["outcome"] == "stale"
    assert [p.name for p in labvms.glob("*.json")] == ["intrus.json"]
    # Aucun nouveau plan calculé automatiquement ; nouvelle approbation exigée.
    assert len(flows.jobs_of(conn, c["id"], "tf_plan")) == 1


def test_t27_seconde_planification_sans_changement(conn, demo, worker, worker_env):
    c, plan, job_id = _approved(conn, demo, worker)
    worker.drain(kinds=["tf_apply"])
    conn.execute("UPDATE jobs SET state = 'cancelled' WHERE change_id = %s AND state = 'queued'", (c["id"],))
    conn.execute("UPDATE changes SET state = 'succeeded' WHERE id = %s", (c["id"],))
    conn.commit()
    service.replan(conn, demo.principal("alice"), c["id"])
    conn.commit()
    worker.drain(kinds=["tf_plan"])
    plan2 = flows.latest_plan(conn, c["id"])
    assert plan2["status"] == "no_changes"
    assert flows.change(conn, c["id"])["state"] == "succeeded"
    last = conn.execute("SELECT * FROM state_transitions WHERE change_id = %s ORDER BY id DESC LIMIT 1",
                        (c["id"],)).fetchone()
    assert last["reason_code"] == "no_changes"
    assert len(list((worker_env / "demo-infra").glob("*/*.json"))) == 1
