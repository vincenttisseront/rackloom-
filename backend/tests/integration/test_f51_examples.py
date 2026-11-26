"""F51 : « Qu'est-ce que je peux demander ? » — exemples filtrés par droits, sans Ollama."""

from __future__ import annotations

API = "/api/v1"


def test_exemples_sans_prod_et_sans_assistant(api, conn, demo):
    # Chloé : projet compta sans environnement de production.
    chloe = api("chloe").get(f"{API}/examples").json()
    assert chloe["assistant_independent"] is True
    assert all(not e["is_production"] for e in chloe["examples"])
    assert all(e["project"]["slug"] == "compta" for e in chloe["examples"])
    assert all(e["can_submit"] and e["can_execute"] is False for e in chloe["examples"])

    # Alice a accès vitrine/prod → peut recevoir des exemples prod.
    alice = api("alice").get(f"{API}/projects/vitrine/examples").json()
    assert any(e["is_production"] for e in alice["examples"])
    assert any(not e["is_production"] for e in alice["examples"])
    assert all(e["need"]["schema"] == "rackloom.need/v1" for e in alice["examples"])
    # Aucun champ technique dans le need.
    for e in alice["examples"]:
        assert set(e["need"]) <= {"schema", "service", "environment", "confirm_production", "inputs"}
        assert set(e["need"]["inputs"]) <= {"quantity", "size_tier", "usage", "lifetime_until"}


def test_exemples_prod_retires_sans_droit(api, conn, demo):
    admin = api("admin")
    alice_user = demo.user("alice")
    # Révoque l'opérateur projet entier, rebind sur dev seulement.
    bindings = conn.execute(
        "SELECT id FROM role_bindings WHERE user_id = %s AND role = 'operator' AND revoked_at IS NULL",
        (alice_user["id"],),
    ).fetchall()
    for b in bindings:
        assert admin.post(f"{API}/role-bindings/{b['id']}/revoke").status_code == 200
    env = conn.execute(
        "SELECT id FROM environments WHERE project_id = %s AND slug = 'dev'",
        (demo.project("vitrine")["id"],),
    ).fetchone()
    grant = admin.post(
        f"{API}/role-bindings",
        json={
            "user_id": str(alice_user["id"]),
            "role": "operator",
            "project_id": str(demo.project("vitrine")["id"]),
            "environment_id": str(env["id"]),
        },
    )
    assert grant.status_code == 200, grant.text

    examples = api("alice").get(f"{API}/projects/vitrine/examples").json()["examples"]
    assert examples
    assert all(not e["is_production"] for e in examples)
    assert all(e["need"]["environment"] != "prod" for e in examples)


def test_hors_perimetre_404(api, conn, demo):
    r = api("chloe").get(f"{API}/projects/vitrine/examples")
    assert r.status_code == 404
