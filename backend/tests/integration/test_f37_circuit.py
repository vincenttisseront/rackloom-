"""F37 : disjoncteur par connecteur.

Connecteur en panne → disjoncteur ouvert ; jobs `write` bloqués `connector_circuit_open` ;
jobs `read` admis ; écriture déjà envoyée → `uncertain` + réconciliation ; fermeture refusée
tant qu'il reste des incertains, puis autorisée après santé OK.
Niveau de preuve : testé localement (PostgreSQL éphémère, Terraform CLI, vCenter fictif).
"""

from __future__ import annotations

import pytest

import flows
from rackloom.connectors import circuit
from rackloom.policy import BUILTIN_VERSION

API = "/api/v1"
REASON = "vCenter de test injoignable"


def _vsphere(conn):
    return conn.execute("SELECT * FROM connectors WHERE slug = 'vsphere-fake'").fetchone()


def _approved(conn, demo, worker, **kw):
    c = flows.create_and_submit(conn, demo, **kw)
    assert flows.drain_plan(worker) == ["preflight", "tf_plan"]
    plan = flows.latest_plan(conn, c["id"])
    res = flows.approve(conn, demo, c["id"], plan)
    return c, plan, res["job_id"]


def _decisions(conn, job_id):
    conn.commit()
    return conn.execute(
        "SELECT stage, outcome, rules FROM admission_decisions WHERE job_id = %s ORDER BY id",
        (job_id,),
    ).fetchall()


@pytest.mark.terraform
def test_f37_write_bloque_read_admet(conn, demo, worker, worker_env, api):
    c, plan, job_id = _approved(conn, demo, worker)
    assert flows.job(conn, job_id)["state"] == "queued"
    vs = _vsphere(conn)
    trip = circuit.open_circuit(conn, vs["id"], REASON, actor_type="user", actor_id=str(demo.user("admin")["id"]))
    conn.commit()
    assert trip["opened"] is True
    assert trip["write_jobs"]["queued"] >= 1

    assert worker.drain(kinds=["tf_apply"]) == ["tf_apply"]
    job = flows.job(conn, job_id)
    assert [job["state"], job["blocked_reason"], job["dispatched_at"]] == [
        "blocked",
        "connector_circuit_open",
        None,
    ]
    assert flows.change(conn, c["id"])["state"] == "blocked"
    decisions = _decisions(conn, job_id)
    assert [(d["stage"], d["outcome"]) for d in decisions] == [("start", "refused")]
    assert decisions[0]["rules"][-1]["id"] == "R095"
    assert decisions[0]["rules"][-1]["code"] == "connector_circuit_open"
    assert not (worker_env / "workspaces" / str(job_id)).exists()

    # Lecture : inventaire encore admis (classe read, R095 non évaluée).
    fake = api("admin").post(f"{API}/connectors/{vs['id']}/inventory-refresh")
    assert fake.status_code == 200, fake.text
    assert fake.json()["simulated"] is True

    view = api("alice").get(f"{API}/changes/{c['id']}").json()
    assert view["state"] == "blocked"
    assert "disjoncteur" in view["reason"].lower() or "R095" in view["reason"]


@pytest.mark.terraform
def test_f37_seuil_ouvre_et_test_ferme(conn, demo, api):
    vs = _vsphere(conn)
    threshold = circuit.failure_threshold()
    for i in range(threshold - 1):
        res = circuit.record_failure(conn, vs["id"], f"échec de sonde {i + 1}")
        assert res["opened"] is False
    conn.commit()
    row = conn.execute("SELECT * FROM connectors WHERE id = %s", (vs["id"],)).fetchone()
    assert row["circuit_state"] == "closed"
    assert row["consecutive_failures"] == threshold - 1

    res = circuit.record_failure(conn, vs["id"], "échec de sonde final")
    conn.commit()
    assert res["opened"] is True
    row = conn.execute("SELECT * FROM connectors WHERE id = %s", (vs["id"],)).fetchone()
    assert row["circuit_state"] == "open"
    assert row["circuit_opened_reason"]

    # Santé OK sans écriture incertaine → fermeture.
    close = circuit.note_success(conn, vs["id"], actor_type="user", actor_id=str(demo.user("admin")["id"]))
    conn.commit()
    assert close["closed"] is True
    row = conn.execute("SELECT * FROM connectors WHERE id = %s", (vs["id"],)).fetchone()
    assert row["circuit_state"] == "closed"
    assert row["consecutive_failures"] == 0
    assert row["circuit_opened_at"] is None

    listed = api("admin").get(f"{API}/connectors").json()
    entry = next(x for x in listed if x["slug"] == "vsphere-fake")
    assert entry["circuit"]["state"] == "closed"


@pytest.mark.terraform
def test_f37_fermeture_refusee_si_uncertain(conn, demo, worker):
    from rackloom.changes.transitions import transition

    c, plan, job_id = _approved(conn, demo, worker)
    # Simule une écriture déjà envoyée devenue incertaine (sans empiler chez le fournisseur).
    conn.execute(
        "UPDATE jobs SET state = 'uncertain', dispatched_at = now(), provider_ref = %s, "
        "lease_owner = NULL WHERE id = %s",
        (f"run-{job_id}", job_id),
    )
    change = flows.change(conn, c["id"])
    transition(conn, change, "executing", reason_code="apply_dispatched", actor_type="worker", actor_id="test")
    change = flows.change(conn, c["id"])
    transition(
        conn,
        change,
        "uncertain",
        reason_code="lease_lost_after_dispatch",
        actor_type="worker",
        actor_id="test",
        detail={"message": "simulé F37"},
    )
    vs = _vsphere(conn)
    circuit.open_circuit(conn, vs["id"], REASON)
    conn.commit()

    close = circuit.note_success(conn, vs["id"], actor_type="user", actor_id=str(demo.user("admin")["id"]))
    conn.commit()
    assert close["closed"] is False
    assert close["blocked_by_uncertain"] >= 1
    row = conn.execute("SELECT circuit_state FROM connectors WHERE id = %s", (vs["id"],)).fetchone()
    assert row["circuit_state"] == "open"

    # Réconciliation aboutie (job plus uncertain) → fermeture possible.
    conn.execute("UPDATE jobs SET state = 'succeeded', finished_at = now() WHERE id = %s", (job_id,))
    change = flows.change(conn, c["id"])
    transition(conn, change, "reconciling", reason_code="reconcile_started", actor_type="worker", actor_id="test")
    change = flows.change(conn, c["id"])
    transition(conn, change, "failed", reason_code="reconciled_not_applied", actor_type="worker", actor_id="test")
    conn.commit()
    assert circuit.uncertain_writes(conn, "vsphere") == 0
    close = circuit.note_success(conn, vs["id"], actor_type="user", actor_id=str(demo.user("admin")["id"]))
    conn.commit()
    assert close["closed"] is True


@pytest.mark.terraform
def test_f37_dispatch_refuse_si_ouvert_apres_admission(conn, demo, worker, worker_env, monkeypatch):
    """Ouverture entre admission et envoi → refus à l'étape dispatch, aucun envoi fournisseur."""
    from rackloom.db import transaction
    from rackloom.worker import handlers

    c, plan, job_id = _approved(conn, demo, worker)
    vs = _vsphere(conn)
    original = handlers._prepare_tf
    opened = {"done": False}

    def prepare_open(r, run_id, resolution, job_json):
        work = original(r, run_id, resolution, job_json)
        if not opened["done"]:
            with transaction() as tx:
                circuit.open_circuit(tx, vs["id"], "Coupure pendant préparation")
            opened["done"] = True
        return work

    monkeypatch.setattr(handlers, "_prepare_tf", prepare_open)
    assert worker.drain(kinds=["tf_apply"]) == ["tf_apply"]
    job = flows.job(conn, job_id)
    assert [job["state"], job["blocked_reason"], job["dispatched_at"]] == [
        "blocked",
        "connector_circuit_open",
        None,
    ]
    stages = [(d["stage"], d["outcome"]) for d in _decisions(conn, job_id)]
    assert stages == [("start", "admitted"), ("dispatch", "refused")]
    assert not (worker_env / "workspaces" / str(job_id)).exists()


def test_f37_policy_bundle_builtin4():
    assert BUILTIN_VERSION == "builtin@9"
    from rackloom import policy

    ids = [r["id"] for r in policy.BUILTIN_RULES["admission"]]
    assert "R095" in ids
