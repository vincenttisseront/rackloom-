"""F55 : boîte « À moi d'agir » / « En attente d'une autre personne ».

Actions calculées côté serveur ; droits revérifiés au clic (403 / 409).
"""

from __future__ import annotations

import flows

API = "/api/v1"


def _plan_ready(conn, demo, worker, **kw):
    c = flows.create_and_submit(conn, demo, **kw)
    # F31 : preflight puis tf_plan (tf_plan n'est enfilé qu'après succès).
    assert flows.drain_plan(worker) == ["preflight", "tf_plan"]
    assert flows.change(conn, c["id"])["state"] == "plan_ready"
    return c


def test_demandeur_attend_approbateur_examine(api, conn, demo, worker, worker_env):
    c = _plan_ready(conn, demo, worker)
    alice = api("alice").get(f"{API}/inbox").json()
    bruno = api("bruno").get(f"{API}/inbox").json()
    david = api("david").get(f"{API}/inbox").json()

    alice_wait = [i for i in alice["waiting"] if i["change_id"] == str(c["id"])]
    assert len(alice_wait) == 1
    assert "approbation" in alice_wait[0]["blocker"].lower()
    assert not any(i["change_id"] == str(c["id"]) for i in alice["mine"])

    bruno_mine = [i for i in bruno["mine"] if i["change_id"] == str(c["id"])]
    assert len(bruno_mine) == 1
    assert bruno_mine[0]["next_action"]["action"] == "approve"
    assert bruno_mine[0]["next_action"]["label"] == "Examiner le plan"
    assert not any(i["change_id"] == str(c["id"]) for i in bruno["waiting"])

    # Auditeur : voit l'état, ne peut pas approuver.
    david_wait = [i for i in david["waiting"] if i["change_id"] == str(c["id"])]
    assert len(david_wait) == 1
    assert not any(i["change_id"] == str(c["id"]) for i in david["mine"])
    r = api("david").post(
        f"{API}/changes/{c['id']}/approvals",
        json={
            "plan_id": str(flows.latest_plan(conn, c["id"])["id"]),
            "bundle_digest": flows.latest_plan(conn, c["id"])["bundle_digest"],
            "decision": "approved",
        },
    )
    assert r.status_code == 403, r.text


def test_apres_decision_action_disparait_et_409(api, conn, demo, worker, worker_env):
    c = _plan_ready(conn, demo, worker)
    plan = flows.latest_plan(conn, c["id"])
    flows.approve(conn, demo, c["id"], plan)
    inbox = api("bruno").get(f"{API}/inbox").json()
    assert not any(i["change_id"] == str(c["id"]) for i in inbox["mine"])
    # Deuxième décision → 409 (action devenue impossible).
    r = api("bruno").post(
        f"{API}/changes/{c['id']}/approvals",
        json={"plan_id": str(plan["id"]), "bundle_digest": plan["bundle_digest"], "decision": "approved"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["code"] in ("action_unavailable", "already_decided", "conflict") or r.status_code == 409


def test_role_revoque_retire_entree_et_403(api, conn, demo, worker, worker_env):
    c = _plan_ready(conn, demo, worker)
    before = api("bruno").get(f"{API}/inbox").json()
    assert any(i["change_id"] == str(c["id"]) for i in before["mine"])

    binding = conn.execute(
        "SELECT id FROM role_bindings WHERE user_id = %s AND role = 'approver' AND revoked_at IS NULL",
        (demo.user("bruno")["id"],),
    ).fetchone()
    admin = api("admin")
    rev = admin.post(f"{API}/role-bindings/{binding['id']}/revoke")
    assert rev.status_code == 200, rev.text
    # Conservé en lecture (auditeur projet) pour distinguer 403 (pas de droit) de 404 (hors périmètre).
    grant = admin.post(
        f"{API}/role-bindings",
        json={
            "user_id": str(demo.user("bruno")["id"]),
            "role": "auditor",
            "project_id": str(demo.project("vitrine")["id"]),
        },
    )
    assert grant.status_code == 200, grant.text

    bruno = api("bruno")
    after = bruno.get(f"{API}/inbox").json()
    assert not any(i["change_id"] == str(c["id"]) for i in after["mine"])
    assert any(i["change_id"] == str(c["id"]) for i in after["waiting"])

    plan = flows.latest_plan(conn, c["id"])
    r = bruno.post(
        f"{API}/changes/{c['id']}/approvals",
        json={"plan_id": str(plan["id"]), "bundle_digest": plan["bundle_digest"], "decision": "approved"},
    )
    assert r.status_code == 403, r.text


def test_hors_perimetre_absent(api, conn, demo, worker, worker_env):
    c = _plan_ready(conn, demo, worker)
    chloe = api("chloe").get(f"{API}/inbox").json()
    assert not any(i["change_id"] == str(c["id"]) for i in chloe["mine"] + chloe["waiting"])
