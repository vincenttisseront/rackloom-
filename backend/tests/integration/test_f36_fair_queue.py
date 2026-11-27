"""F36 : file d'exécution équitable — quotas par projet, raison d'attente.

Une campagne qui sature son quota n'empêche pas un autre projet de démarrer.
Niveau de preuve : testé localement.
"""

from __future__ import annotations

from rackloom.db import transaction
from rackloom.executions import queue
from rackloom.executions import quotas as fair
from rackloom.ids import uuid7
from rackloom.policy import BUILTIN_VERSION


def _queued_job(conn, *, project_id, change_id=None, kind="tf_apply", priority=100, key=None):
    return queue.enqueue(
        conn,
        kind=kind,
        idempotency_key=key or f"f36-{uuid7()}",
        project_id=project_id,
        change=None if change_id is None else {"id": change_id, "project_id": project_id},
        priority=priority,
        payload={"test": True},
    )


def test_f36_autre_projet_demarre_malgre_quota(conn, demo):
    """20 jobs projet A (quota 2) + 1 job projet B → B démarre sans attendre."""
    assert BUILTIN_VERSION == "builtin@9"
    vit = demo.project("vitrine")["id"]
    cpt = demo.project("compta")["id"]
    # Même priorité : les jobs vitrine sont en tête de file ; le claim doit les
    # sauter (quota saturé) et prendre le job compta.
    fair.set_quota(conn, vit, job_class="write", max_concurrent=2, priority=100)
    fair.set_quota(conn, cpt, job_class="write", max_concurrent=10, priority=100)
    conn.commit()

    # Saturation : 2 jobs vitrine déjà actifs.
    for i in range(2):
        j = _queued_job(conn, project_id=vit, key=f"f36-active-{i}", priority=100)
        conn.execute(
            "UPDATE jobs SET state = 'running', lease_owner = 'w', lease_epoch = 1 WHERE id = %s",
            (j["id"],),
        )
    waiting = [_queued_job(conn, project_id=vit, key=f"f36-wait-{i}", priority=100) for i in range(18)]
    other = _queued_job(conn, project_id=cpt, key="f36-other", priority=100)
    conn.commit()

    with transaction() as tx:
        claimed = queue.claim(tx, "worker-fair", 30, kinds=["tf_apply"])
    assert claimed is not None
    assert claimed["id"] == other["id"]
    assert claimed["project_id"] == cpt

    for j in waiting:
        row = conn.execute("SELECT wait_reason, state FROM jobs WHERE id = %s", (j["id"],)).fetchone()
        assert row["state"] == "queued"
        assert row["wait_reason"] == fair.WAIT_QUOTA


def test_f36_estimate_expose_raison(conn, demo, api):
    vit = demo.project("vitrine")["id"]
    env = demo.env("vitrine", "dev")
    fair.set_quota(conn, vit, job_class="write", max_concurrent=1)
    alice = demo.user("alice")["id"]
    cid = uuid7()
    conn.execute(
        "INSERT INTO changes (id, project_id, environment_id, kind, title, state, created_by) "
        "VALUES (%s, %s, %s, 'create', 'F36 ETA', 'approved', %s)",
        (cid, vit, env["id"], alice),
    )
    active = _queued_job(conn, project_id=vit, change_id=cid, key="f36-eta-active")
    conn.execute(
        "UPDATE jobs SET state = 'running', lease_owner = 'w', lease_epoch = 1 WHERE id = %s",
        (active["id"],),
    )
    waiting = _queued_job(conn, project_id=vit, change_id=cid, key="f36-eta-wait")
    conn.commit()

    # Marque l'attente via claim (projet saturé → skip).
    with transaction() as tx:
        queue.claim(tx, "w", 30, kinds=["tf_apply"])
    row = conn.execute("SELECT wait_reason FROM jobs WHERE id = %s", (waiting["id"],)).fetchone()
    assert row["wait_reason"] == fair.WAIT_QUOTA

    body = api("alice").get(f"/api/v1/changes/{cid}/estimate").json()
    assert body["queue"]["status"] == "waiting"
    assert body["queue"]["wait_reason"] == fair.WAIT_QUOTA
    assert body["queue"]["runner_choice"] is False
    assert "quota" in (body["queue"]["reason"] or "").lower() or "satur" in (body["queue"]["reason"] or "").lower()


def test_f36_pas_de_choix_runner_dans_api(api, demo):
    """L'estimation n'expose aucun champ de sélection de runner."""
    change = api("alice").post("/api/v1/projects/vitrine/changes", json={"title": "F36 runner"}).json()
    body = api("alice").get(f"/api/v1/changes/{change['id']}/estimate").json()
    assert "runner" not in body["queue"] or body["queue"].get("runner_choice") is False
    assert body["model_used"] is False
