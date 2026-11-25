"""Parcours par l'API publique : besoin, révision, plan réel, approbation, `allowed_actions`.

Couvre T05 (Idempotency-Key), T08 par l'API, T32 (API et client CI), T39 par l'API, la revérification
de `allowed_actions` au moment de l'appel et l'export OpenAPI.
Niveau de preuve : testé localement (TestClient HTTPS, Terraform CLI réel, launcher en mode processus).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import flows
from rackloom import cli
from rackloom.ci_client import CiClient, CiError

API = "/api/v1"
ROOT = Path(__file__).resolve().parents[3]
LOCKED = {
    "inputs": [("image", "img-ubuntu-2404-v3"), ("network", "net-dev-vitrine-a"), ("vcpu", 2),
               ("datastore", "ds-lab-01"), ("ip", "192.0.2.10"), ("vlan", 12), ("role_version", "1.0.0")],
    "need": [("cluster", "cl-lab"), ("folder", "vitrine"), ("network", "net-dev-vitrine-a")],
}


def _new(client, need=None, project="vitrine") -> dict:
    change = client.post(f"{API}/projects/{project}/changes", json={"title": "Site vitrine"}).json()
    r = client.post(f"{API}/changes/{change['id']}/versions",
                    json={"need": need or flows.need(), "base_version_no": 0, "source": "webui"})
    assert r.status_code == 200, r.text
    return r.json()


def _plan_ready(api, worker, **need_kw):
    alice = api("alice")
    v = _new(alice, flows.need(**need_kw))
    cid = v["change"]["id"]
    assert "submit" in [a["action"] for a in v["change"]["allowed_actions"]]
    r = alice.post(f"{API}/changes/{cid}/submit", json={"revision_no": v["revision_no"]})
    assert r.status_code == 200, r.text
    assert worker.drain(kinds=["tf_plan"]) == ["tf_plan"]
    return alice, cid


def test_brouillon_resume_et_details_du_plan(api):
    alice = api("alice")
    services = alice.get(f"{API}/projects/vitrine/services").json()
    assert [s["slug"] for s in services] == ["ubuntu-web"]
    assert {e["slug"] for e in services[0]["environments"]} == {"dev"}
    assert services[0]["production_note"].startswith("sur demande explicite")
    v = _new(alice)
    change = v["change"]
    assert change["state"] == "draft" and change["revision"]["outcome"] == "resolved"
    assert change["revision"]["summary"]["headline"].startswith("1 serveur web Ubuntu 24.04 avec Nginx")
    groups = {g["group"]: g for g in change["revision"]["details"]["groups"]}
    assert {"placement", "reseau", "dimensionnement"} <= set(groups)
    net = next(v for v in groups["reseau"]["values"] if v["pointer"] == "/placement/network")
    assert net["source"] in ("environment_policy", "tie_break", "inventory")
    assert {s["key"] for s in change["revision"]["details"]["simulated_steps"]} >= {"vm_inventory"}
    assert {a["action"] for a in change["allowed_actions"]} == {"edit", "submit", "cancel", "duplicate"}


def test_t32_api_et_client_ci(api, conn):
    alice = api("alice")
    change = alice.post(f"{API}/projects/vitrine/changes", json={}).json()
    for where, items in LOCKED.items():
        for key, value in items:
            need = flows.need()
            if where == "inputs":
                need["inputs"][key] = value
            else:
                need[key] = value
            r = alice.post(f"{API}/changes/{change['id']}/versions", json={"need": need, "base_version_no": 0})
            assert r.status_code == 422 and r.json()["code"] == "locked_field", (key, r.text)
        r = alice.post(f"{API}/changes/{change['id']}/versions",
                       json={"need": flows.need(), "base_version_no": 0, "network": "net-dev-vitrine-a"})
        assert r.status_code == 422 and r.json()["code"] == "locked_field"
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM request_versions WHERE change_id = %s",
                        (change["id"],)).fetchone()["n"] == 0

    from conftest import DEMO_PASSWORD

    ci = CiClient(api())
    ci.login("alice@demo.invalid", DEMO_PASSWORD)
    with pytest.raises(CiError) as err:
        ci.submit_need("vitrine", {**flows.need(), "inputs": {**flows.need()["inputs"], "vcpu": 2}})
    assert err.value.status == 422 and err.value.body["code"] == "locked_field"
    ok = ci.submit_need("vitrine", flows.need())
    assert ok["outcome"] == "resolved"


def test_t39_duplication_par_api(api):
    alice = api("alice")
    v = _new(alice, flows.need(quantity=2))
    r = alice.post(f"{API}/changes/{v['change']['id']}/duplicate", json={})
    assert r.status_code == 200, r.text
    copy = r.json()["change"]
    assert copy["id"] != v["change"]["id"] and copy["revision"]["outcome"] == "resolved"
    assert copy["need"]["inputs"]["quantity"] == 2
    names = [i["name"] for i in copy["revision"]["instances"]]
    assert names == ["vit-dev-web-01", "vit-dev-web-02"]


@pytest.mark.terraform
def test_t05_t08_approbation_par_api(api, worker, conn):
    alice, cid = _plan_ready(api, worker)
    bruno = api("bruno")
    view = bruno.get(f"{API}/changes/{cid}").json()
    assert view["state"] == "plan_ready"
    plan = view["plan"]
    assert plan["counts"]["create"] == 1 and plan["vms"][0]["name"] == "vit-dev-web-01"
    assert plan["headline"] == "1 VM de labo à créer, 0 modification, 0 suppression, 0 remplacement"
    assert plan["ansible"]["components"][0]["slug"] == "nginx"
    assert plan["ansible"]["components"][0]["ports"] == [{"port": "80", "proto": "tcp"}]
    assert any("HTTP" in v for v in plan["verifications"])
    assert {a["action"] for a in view["allowed_actions"]} >= {"approve", "reject"}
    # Alice voit le plan mais n'a ni approuver ni refuser ; l'appel direct est refusé (T08).
    alice_view = alice.get(f"{API}/changes/{cid}").json()
    assert not {"approve", "reject"} & {a["action"] for a in alice_view["allowed_actions"]}
    body = {"plan_id": plan["id"], "bundle_digest": plan["bundle_digest"], "decision": "approved"}
    assert alice.post(f"{API}/changes/{cid}/approvals", json=body).status_code == 403

    # Empreinte obsolète : 409.
    r = bruno.post(f"{API}/changes/{cid}/approvals", json={**body, "bundle_digest": "sha256:" + "0" * 64})
    assert r.status_code == 409
    # T05 : même Idempotency-Key, même approbation, un seul job.
    first = bruno.post(f"{API}/changes/{cid}/approvals", json=body, headers={"Idempotency-Key": "k-approve-1"})
    assert first.status_code == 200, first.text
    again = bruno.post(f"{API}/changes/{cid}/approvals", json=body, headers={"Idempotency-Key": "k-approve-1"})
    assert again.status_code == 200 and again.json()["approval_id"] == first.json()["approval_id"]
    assert len(flows.jobs_of(conn, cid, "tf_apply")) == 1
    other = bruno.post(f"{API}/changes/{cid}/approvals", json={**body, "comment": "autre"},
                       headers={"Idempotency-Key": "k-approve-1"})
    assert other.status_code == 422 and other.json()["code"] == "idempotency_key_reused"
    # Nouvelle clé, même plan : l'action n'est plus disponible (revérifiée à l'appel).
    r = bruno.post(f"{API}/changes/{cid}/approvals", json=body, headers={"Idempotency-Key": "k-approve-2"})
    assert r.status_code == 409 and r.json()["code"] == "action_unavailable"
    assert worker.drain(kinds=["tf_apply"]) == ["tf_apply"]
    view = alice.get(f"{API}/changes/{cid}").json()
    assert view["deployment"]["status"] == "active"
    assert view["deployment"]["service_status"] == "unknown"
    assert view["deployment"]["service_status_label"] == "Non vérifié"
    jobs = alice.get(f"{API}/changes/{cid}/jobs").json()
    assert [j["kind"] for j in jobs][:3] == ["tf_plan", "tf_apply", "guest_ready"]


@pytest.mark.terraform
def test_allowed_actions_revérifiees_a_l_appel(api, worker):
    alice, cid = _plan_ready(api, worker)
    bruno = api("bruno")
    plan = bruno.get(f"{API}/changes/{cid}").json()["plan"]
    # Bruno a chargé l'écran ; Alice dépose une nouvelle version entre-temps.
    r = alice.post(f"{API}/changes/{cid}/versions",
                   json={"need": flows.need(quantity=2), "base_version_no": 1, "source": "webui"})
    assert r.status_code == 200, r.text
    body = {"plan_id": plan["id"], "bundle_digest": plan["bundle_digest"], "decision": "approved"}
    r = bruno.post(f"{API}/changes/{cid}/approvals", json=body)
    assert r.status_code == 409 and r.json()["code"] == "action_unavailable"
    assert r.json()["details"] == {"action": "approve"}
    # Base de version obsolète : 409 stale_version.
    r = alice.post(f"{API}/changes/{cid}/versions", json={"need": flows.need(), "base_version_no": 1})
    assert r.status_code == 409 and r.json()["code"] == "stale_version"


@pytest.mark.terraform
def test_annulation_de_job_par_api(api, worker):
    alice, cid = _plan_ready(api, worker)
    jobs = alice.get(f"{API}/changes/{cid}/jobs").json()
    r = alice.post(f"{API}/jobs/{jobs[0]['id']}/cancel")
    assert r.status_code == 409 and r.json()["code"] == "action_unavailable"


def test_openapi_exporte_et_a_jour(tmp_path):
    out = tmp_path / "openapi.json"
    assert cli.main(["openapi", "export", "--output", str(out)]) == 0
    spec = json.loads(out.read_text())
    assert "/api/v1/changes/{change_id}/approvals" in spec["paths"]
    committed = ROOT / "openapi" / "openapi.json"
    assert committed.exists(), "exécuter `rackloom openapi export`"
    assert json.loads(committed.read_text()) == spec, "openapi/openapi.json n'est pas à jour"
