"""F53 : détection des demandes déjà ouvertes — périmètre strict (U1), pas de fusion."""

from __future__ import annotations

import json

import flows

API = "/api/v1"


def _create_open(api, who: str, project: str, title: str, **need_kw):
    client = api(who)
    change = client.post(f"{API}/projects/{project}/changes", json={"title": title}).json()
    res = client.post(
        f"{API}/changes/{change['id']}/versions",
        json={"need": flows.need(**need_kw), "base_version_no": 0, "source": "webui"},
    )
    assert res.status_code == 200, res.text
    return change


def test_demande_similaire_signalee(api, conn, demo):
    existing = _create_open(api, "alice", "vitrine", "Serveur web existant", environment="dev")
    r = api("alice").post(
        f"{API}/projects/vitrine/changes/similar",
        json={"need": flows.need(environment="dev")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] >= 1
    assert any(m["change_id"] == existing["id"] for m in body["matches"])
    assert "Aucune fusion" in body["notice"]


def test_hors_perimetre_reponse_identique(api, conn, demo):
    """Demande inaccessible : ni titre, ni compteur, ni message distinct (U1)."""
    _create_open(api, "alice", "vitrine", "Secret vitrine", environment="dev")

    # Accès direct au projet hors périmètre → 404 (pas 403 avec indice).
    denied = api("chloe").post(
        f"{API}/projects/vitrine/changes/similar",
        json={"need": flows.need(environment="dev")},
    )
    assert denied.status_code == 404

    # Sur son projet : créer une demande, puis une demande vitrine supplémentaire ;
    # la réponse compta ne révèle rien de vitrine.
    _create_open(api, "chloe", "compta", "Serveur compta", environment="dev")
    before = api("chloe").post(
        f"{API}/projects/compta/changes/similar",
        json={"need": flows.need(environment="dev")},
    )
    _create_open(api, "alice", "vitrine", "Charge vitrine", environment="dev")
    after = api("chloe").post(
        f"{API}/projects/compta/changes/similar",
        json={"need": flows.need(environment="dev")},
    )
    assert before.status_code == 200 and after.status_code == 200
    assert set(before.json()) == set(after.json()) == {"matches", "count", "notice"}
    assert before.json()["notice"] == after.json()["notice"]
    # Le compteur ne saute pas à cause de la demande vitrine.
    assert after.json()["count"] == before.json()["count"]
    blob = json.dumps(after.json())
    assert "Secret vitrine" not in blob
    assert "Charge vitrine" not in blob
    assert "vitrine" not in blob.lower()


def test_poursuite_separee_pas_de_fusion(api, conn, demo):
    existing = _create_open(api, "alice", "vitrine", "Première", environment="dev")
    second = _create_open(api, "alice", "vitrine", "Seconde volontaire", environment="dev")
    assert existing["id"] != second["id"]
    body = api("alice").post(
        f"{API}/projects/vitrine/changes/similar",
        json={"need": flows.need(environment="dev"), "exclude_change_id": second["id"]},
    ).json()
    assert any(m["change_id"] == existing["id"] for m in body["matches"])
    assert all(m["change_id"] != second["id"] for m in body["matches"])


def test_departage_recent_avant_ancien(api, conn, demo):
    """À score égal, les demandes les plus récentes restent dans le top N (défaut 10)."""
    old_ids = [
        _create_open(api, "alice", "vitrine", f"Ancienne {i}", environment="dev")["id"]
        for i in range(12)
    ]
    recent = _create_open(api, "alice", "vitrine", "Toute récente", environment="dev")
    body = api("alice").post(
        f"{API}/projects/vitrine/changes/similar",
        json={"need": flows.need(environment="dev")},
    ).json()
    ids = [m["change_id"] for m in body["matches"]]
    assert recent["id"] in ids
    assert body["count"] == 10
    # Au moins une des plus anciennes a dû céder sa place.
    assert sum(1 for oid in old_ids if oid in ids) <= 9
