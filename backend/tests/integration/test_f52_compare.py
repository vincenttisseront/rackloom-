"""F52 : comparateur de choix métier — options autorisées, critères non vérifiés omis."""

from __future__ import annotations

import flows

API = "/api/v1"


def test_un_seul_profil_pas_de_comparaison_service(api, conn, demo):
    # Sans ubuntu-web-durci publié : un seul service → mode auto.
    r = api("alice").post(
        f"{API}/projects/vitrine/comparisons",
        json={"need": {"schema": "rackloom.need/v1", "service": {"template": None},
                       "environment": None, "confirm_production": False, "inputs": {}}},
    )
    # Besoin partiel sans template valide : on envoie un need minimal via l'API.
    # L'endpoint accepte un need incomplet côté compare (pas via validate_need).
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "auto"
    assert body["dimension"] == "service"
    assert len(body["options"]) <= 1
    assert "cost" in body["omitted_criteria"]


def test_deux_profils_comparaison_avec_recommandation(api, conn, demo):
    flows.publish_hardened(conn, demo)
    r = api("alice").post(
        f"{API}/projects/vitrine/comparisons",
        json={"need": {"schema": "rackloom.need/v1", "service": {"template": None},
                       "environment": None, "confirm_production": False, "inputs": {}}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "compare"
    assert body["dimension"] == "service"
    keys = {o["key"] for o in body["options"]}
    assert "service:ubuntu-web" in keys
    assert "service:ubuntu-web-durci" in keys
    assert body["recommendation"]["key"] in keys
    # Aucun critère inventé.
    for o in body["options"]:
        assert o["criteria"] == {}
        assert "cost" in o["omitted_criteria"]


def test_niveaux_comparaison_et_choix_fige(api, conn, demo):
    alice = api("alice")
    r = alice.post(
        f"{API}/projects/vitrine/comparisons",
        json={"need": flows.need(environment="dev")},  # size_tier absent
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "compare"
    assert body["dimension"] == "size_tier"
    assert 2 <= len(body["options"]) <= 3

    fixed = alice.post(
        f"{API}/projects/vitrine/comparisons",
        json={"need": flows.need(environment="dev", size_tier="test_leger")},
    ).json()
    assert fixed["mode"] == "auto"
    assert fixed["options"] == []
