"""F54 : passer la main — collègue habilité, version ancrée, aucun role_binding."""

from __future__ import annotations

import flows

API = "/api/v1"


def _draft_with_version(api):
    alice = api("alice")
    change = alice.post(f"{API}/projects/vitrine/changes", json={"title": "Revue F54"}).json()
    res = alice.post(
        f"{API}/changes/{change['id']}/versions",
        json={"need": flows.need(environment="dev"), "base_version_no": 0, "source": "webui"},
    )
    assert res.status_code == 200, res.text
    return change


def test_sollicitation_et_reponse(api, conn, demo):
    change = _draft_with_version(api)
    bruno = demo.user("bruno")
    alice = api("alice")

    cands = alice.get(f"{API}/changes/{change['id']}/review-candidates").json()
    assert any(c["id"] == str(bruno["id"]) for c in cands)

    before_bindings = conn.execute("SELECT count(*) AS n FROM role_bindings").fetchone()["n"]
    created = alice.post(
        f"{API}/changes/{change['id']}/reviews",
        json={
            "solicited_user_id": str(bruno["id"]),
            "question": "Le niveau Test léger te convient-il ?",
            "focus": "/inputs/size_tier",
        },
    )
    assert created.status_code == 200, created.text
    review = created.json()
    assert review["status"] == "open"
    assert review["version_no"] == 1
    assert "ne vaut pas approbation" in review["notice"]
    assert conn.execute("SELECT count(*) AS n FROM role_bindings").fetchone()["n"] == before_bindings

    reply = api("bruno").post(
        f"{API}/changes/{change['id']}/reviews/{review['id']}/replies",
        json={"body": "Oui, garder Test léger."},
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["status"] == "replied"
    assert reply.json()["reply_text"] == "Oui, garder Test léger."

    listed = alice.get(f"{API}/changes/{change['id']}/reviews").json()
    assert listed["items"][0]["status"] == "replied"


def test_non_habilite_refuse_et_404(api, conn, demo):
    change = _draft_with_version(api)
    chloe = demo.user("chloe")
    r = api("alice").post(
        f"{API}/changes/{change['id']}/reviews",
        json={"solicited_user_id": str(chloe["id"]), "question": "Peux-tu regarder ?"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "not_eligible"

    assert api("chloe").get(f"{API}/changes/{change['id']}/reviews").status_code == 404
    assert api("chloe").get(f"{API}/changes/{change['id']}").status_code == 404


def test_nouvelle_version_ecarts(api, conn, demo):
    alice = api("alice")
    change = alice.post(f"{API}/projects/vitrine/changes", json={"title": "Écarts F54"}).json()
    v1 = alice.post(
        f"{API}/changes/{change['id']}/versions",
        json={"need": flows.need(environment="dev", size_tier="test_leger"), "base_version_no": 0},
    )
    assert v1.status_code == 200, v1.text
    bruno = demo.user("bruno")
    review = alice.post(
        f"{API}/changes/{change['id']}/reviews",
        json={"solicited_user_id": str(bruno["id"]), "question": "OK pour test léger ?"},
    ).json()

    v2 = alice.post(
        f"{API}/changes/{change['id']}/versions",
        json={
            "need": flows.need(environment="dev", size_tier="standard"),
            "base_version_no": 1,
            "source": "webui",
        },
    )
    assert v2.status_code == 200, v2.text

    listed = alice.get(f"{API}/changes/{change['id']}/reviews").json()
    diffs = listed["diffs_since_last_review"]
    assert diffs["has_newer_version"] is True
    assert diffs["version_no_then"] == review["version_no"]
    assert diffs["version_no_now"] == 2
    assert diffs["notes"]
