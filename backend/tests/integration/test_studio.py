"""Atelier AT01 : brouillon assisté, revue, publication. Aucune route ne crée de job.

Niveau de preuve : testé localement (API réelle, PostgreSQL éphémère, adaptateur fictif).
Le modèle Ollama n'est pas interrogé : mêmes contrôles serveur, appel non vérifié.
"""

from __future__ import annotations

import flows
from rackloom import chat

API = "/api/v1"
BASE = {"slug": "ubuntu-web", "version": "1.0.0"}
BRIEF = "Je veux un serveur web Ubuntu durci, avec Fail2ban sur SSH, déployé une machine d'abord"


def _draft(api, brief: str | None = BRIEF) -> tuple[object, dict]:
    ines = api("ines")
    body: dict = {"slug": "ubuntu-web-durci", "base": BASE}
    if brief is not None:
        body["brief"] = brief
    created = ines.post(f"{API}/studio/drafts", json=body)
    assert created.status_code == 200, created.text
    return ines, created.json()


def _accept_all(ines, draft: dict) -> dict:
    for _ in range(12):
        pending = [s for s in draft["suggestions"] if s["status"] == "pending"]
        if not pending:
            return draft
        decided = ines.post(
            f"{API}/studio/drafts/{draft['id']}/suggestions/{pending[0]['id']}",
            json={"row_version": draft["row_version"], "decision": "accepted"},
        )
        assert decided.status_code == 200, decided.text
        draft = decided.json()
    raise AssertionError("suggestions sans fin")


def test_parcours_brouillon_revue_publication(api, conn):
    ines, created = _draft(api)
    assert created["assistant"]["simulated"] is True and created["assistant"]["status"] == "ok"
    assert "Fail2ban" in created["assistant"]["reply"] or "réglage" in created["assistant"]["reply"]
    draft = _accept_all(ines, created["draft"])
    assert draft["design"]["waves"]["mode"] == "pilot"
    assert {c["component"] for c in draft["design"]["components"]} == {"nginx", "fail2ban"}
    assert any(p["source"] == "assistant" for p in draft["provenance"])
    assert draft["allowed_actions"]["submit"] is None
    assert not draft["problems"]
    # Une suggestion en attente bloque ; ici tout est décidé.
    submitted = ines.post(
        f"{API}/studio/drafts/{draft['id']}/submit",
        json={"row_version": draft["row_version"], "digest": draft["digest"]},
    )
    assert submitted.status_code == 200, submitted.text
    version, digest = submitted.json()["version"], submitted.json()["digest"]
    jobs = conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]

    denied = ines.post(
        f"{API}/admin/templates/ubuntu-web-durci/versions/{version}/publish",
        json={"reason": "je publie moi-même", "digest": digest},
    )
    assert denied.status_code == 403

    pub = api("pub")
    stale = pub.post(
        f"{API}/admin/templates/ubuntu-web-durci/versions/{version}/publish",
        json={"reason": "empreinte périmée", "digest": "sha256:" + "0" * 64},
    )
    assert stale.status_code == 409 and stale.json()["code"] == "stale_digest"
    detail = pub.get(f"{API}/admin/templates/ubuntu-web-durci/versions/{version}").json()
    assert detail["review"]["author"].startswith("Inès")
    assert any("fail2ban" in c for c in detail["review"]["changes_from_base"])
    assert "publish" in detail["allowed_actions"] and "return" in detail["allowed_actions"]
    # L'auteur qui consulte sa version soumise ne peut pas la publier (séparation affichée).
    own = ines.get(f"{API}/admin/templates/ubuntu-web-durci/versions/{version}").json()
    assert own["allowed_actions"] == [] and own["separation_notice"]

    published = pub.post(
        f"{API}/admin/templates/ubuntu-web-durci/versions/{version}/publish",
        json={"reason": "Revue de la première tranche", "digest": digest},
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == jobs
    assert (
        conn.execute("SELECT status FROM template_versions WHERE slug = 'ubuntu-web' AND version = '1.0.0'").fetchone()[
            "status"
        ]
        == "published"
    )


def test_auteur_distinct_du_publicateur(api, conn, demo):
    """Même si le publicateur conçoit, il ne publie pas sa propre soumission."""
    from rackloom.ids import uuid7

    pub_user, admin = demo.user("pub"), demo.user("admin")
    conn.execute(
        "INSERT INTO role_bindings (id, user_id, role, granted_by) VALUES (%s, %s, 'template_author', %s)",
        (uuid7(), pub_user["id"], admin["id"]),
    )
    conn.commit()
    pub = api("pub")
    created = pub.post(f"{API}/studio/drafts", json={"slug": "ubuntu-web-durci", "base": BASE, "brief": BRIEF})
    assert created.status_code == 200, created.text
    draft = _accept_all(pub, created.json()["draft"])
    submitted = pub.post(
        f"{API}/studio/drafts/{draft['id']}/submit",
        json={"row_version": draft["row_version"], "digest": draft["digest"]},
    )
    assert submitted.status_code == 200, submitted.text
    version, digest = submitted.json()["version"], submitted.json()["digest"]
    refused = pub.post(
        f"{API}/admin/templates/ubuntu-web-durci/versions/{version}/publish",
        json={"reason": "auto-publication", "digest": digest},
    )
    assert refused.status_code == 403 and refused.json()["code"] == "separation_of_duties"
    returned = pub.post(
        f"{API}/admin/templates/ubuntu-web-durci/versions/{version}/return",
        json={"reason": "préciser la description", "digest": digest},
    )
    assert returned.status_code == 403 and returned.json()["code"] == "separation_of_duties"


def test_renvoi_puis_nouvelle_version(api):
    ines, created = _draft(api)
    draft = _accept_all(ines, created["draft"])
    submitted = ines.post(
        f"{API}/studio/drafts/{draft['id']}/submit",
        json={"row_version": draft["row_version"], "digest": draft["digest"]},
    )
    version, digest = submitted.json()["version"], submitted.json()["digest"]
    pub = api("pub")
    returned = pub.post(
        f"{API}/admin/templates/ubuntu-web-durci/versions/{version}/return",
        json={"reason": "ajouter la description du pilote", "digest": digest},
    )
    assert returned.status_code == 200 and returned.json()["status"] == "returned"
    again = ines.get(f"{API}/studio/drafts/{draft['id']}").json()
    assert again["status"] == "editing" and again["version"] != version
    assert again["returned"]["reason"] == "ajouter la description du pilote"
    edited = ines.post(
        f"{API}/studio/drafts/{draft['id']}/edit",
        json={
            "row_version": again["row_version"],
            "ops": [{"path": "/description", "value": "Ubuntu 24.04, Nginx et Fail2ban, pilote puis vagues"}],
        },
    )
    assert edited.status_code == 200, edited.text
    draft = edited.json()
    submitted = ines.post(
        f"{API}/studio/drafts/{draft['id']}/submit",
        json={"row_version": draft["row_version"], "digest": draft["digest"]},
    )
    assert submitted.status_code == 200 and submitted.json()["version"] == again["version"]


def test_suggestion_hors_conception_rejetee_sans_effet(api, conn, demo):
    chat.set_assistant(conn, demo.principal("admin"), "fake", "override_attempt")
    conn.commit()
    ines, created = _draft(api, brief="mets les serveurs sur le réseau DMZ")
    assert created["assistant"]["status"] == "rejected"
    assert created["assistant"]["code"] == "out_of_scope_path"
    draft = created["draft"]
    comps = draft["design"]["components"]
    assert [c["component"] for c in comps] == ["nginx"]
    assert comps[0]["verification_level"] == "not_tested" and comps[0]["evidence"]["count"] == 0
    assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0
    edited = ines.post(
        f"{API}/studio/drafts/{draft['id']}/edit",
        json={"row_version": draft["row_version"], "ops": [{"path": "/placement/network", "value": "dmz"}]},
    )
    assert edited.status_code == 422 and edited.json()["code"] == "path_not_allowed"


def test_parametre_inconnu_devient_une_proposition(api, conn, demo):
    ines, created = _draft(api, brief=None)
    draft = created["draft"]
    out = ines.post(
        f"{API}/studio/drafts/{draft['id']}/edit",
        json={
            "row_version": draft["row_version"],
            "ops": [{"path": "/components/fail2ban/config/pays", "value": "FR"}],
        },
    )
    assert out.status_code == 422 and out.json()["code"] == "invalid_value"
    asked = ines.post(
        f"{API}/studio/drafts/{draft['id']}/assist",
        json={"row_version": draft["row_version"], "message": "Il me faudrait un antivirus ClamAV en plus"},
    )
    assert asked.status_code == 200, asked.text
    body = asked.json()
    assert body["status"] == "ok" and body["draft"]["extensions"]
    assert body["draft"]["extensions"][0]["kind"] == "new_component"
    assert "fail2ban" not in {c["component"] for c in body["draft"]["design"]["components"]}
    assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0
    bounded = ines.post(
        f"{API}/studio/drafts/{draft['id']}/edit",
        json={
            "row_version": body["draft"]["row_version"],
            "ops": [
                {"path": "/components/fail2ban", "value": {"version": "1.0.0"}},
                {"path": "/components/fail2ban/config/maxretry", "value": 99},
            ],
        },
    )
    assert bounded.status_code == 422 and bounded.json()["code"] == "invalid_value"


def test_operateur_sans_acces_a_l_atelier_ni_au_reglage(api, conn, demo):
    alice = api("alice")
    assert alice.get(f"{API}/studio/drafts").status_code == 403
    assert alice.post(f"{API}/studio/drafts", json={"slug": "ubuntu-web-durci", "base": BASE}).status_code == 403
    flows.publish_hardened(conn, demo)
    change = alice.post(f"{API}/projects/vitrine/changes", json={"title": "contournement"}).json()
    need = flows.need(template="ubuntu-web-durci", quantity=2)
    need["inputs"]["maxretry"] = 3
    refused = alice.post(
        f"{API}/changes/{change['id']}/versions", json={"need": need, "base_version_no": 0, "source": "webui"}
    )
    assert refused.status_code == 422 and refused.json()["code"] == "locked_field"
    ok = alice.post(
        f"{API}/changes/{change['id']}/versions",
        json={"need": flows.need(template="ubuntu-web-durci", quantity=2), "base_version_no": 0, "source": "webui"},
    )
    assert ok.status_code == 200, ok.text
    resolution = ok.json()["change"]["revision"]
    assert resolution["outcome"] == "resolved"
    assert "pilote" in resolution["summary"]["waves"]
    assert "jail" in resolution["summary"]["verifications"]
    chloe = api("chloe")
    draft_id = conn.execute("SELECT id FROM template_drafts").fetchone()["id"]
    assert chloe.get(f"{API}/studio/drafts/{draft_id}").status_code == 404


def test_composant_inconnu_refuse_par_le_serveur(api, conn, demo):
    chat.set_assistant(conn, demo.principal("admin"), "fake", "unknown_option")
    conn.commit()
    _ines, created = _draft(api, brief="ajoute Apache")
    assert created["assistant"]["status"] == "rejected"
    assert created["assistant"]["code"] == "invalid_value"
