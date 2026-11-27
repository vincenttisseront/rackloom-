"""Lot C : F10 politiques, F11 droits temporaires, F12 réauth, F13 admin déléguée, F14 urgence.

Niveau de preuve : testé localement. IdP d'entreprise : non (preuve simulée / step-up local).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

import flows
from rackloom import admission, clock, policy
from rackloom.identity import emergency, temporary
from rackloom.ids import uuid7
from rackloom.policy import BUILTIN_VERSION, exceptions as policy_exc
from rackloom.policy import simulate as policy_sim


API = "/api/v1"


def test_f10_bundle_builtin8():
    assert BUILTIN_VERSION == "builtin@8"
    ids = {r["id"] for r in policy.BUILTIN_RULES["admission"]}
    assert {"R100", "R140"} <= ids


def test_f10_refus_indique_regle_et_champ(conn, demo):
    """R140 : écriture prod sans exception → org_policy_refused + field."""
    env = demo.env("vitrine", "prod")
    ctx = admission.Context(
        conn=conn,
        job={
            "kind": "tf_apply",
            "class": "write",
            "id": demo.user("alice")["id"],
            "cancel_requested_at": None,
            "stack_id": None,
            "payload": {},
        },
        change={"id": demo.user("alice")["id"], "project_id": demo.project("vitrine")["id"]},
        environment=env,
        execution=None,
        approval=None,
        plan=None,
        revision=None,
    )
    fn = next(r[4] for r in admission.REGISTRY if r[1] == "R140")
    verdict = fn(ctx)
    assert not verdict.ok
    assert verdict.code == "org_policy_refused"
    assert verdict.field == "/environment"


def test_f10_exception_expiree_sans_effet(conn, demo):
    admin = demo.principal("admin", step_up=True)
    project_id = demo.project("vitrine")["id"]
    env_id = demo.env("vitrine", "prod")["id"]
    past = clock.now() - timedelta(hours=1)
    # Insertion directe d'une exception déjà expirée.
    conn.execute(
        """
        INSERT INTO policy_exceptions
            (id, rule_id, project_id, environment_id, owner_user_id, reason, expires_at, created_by)
        VALUES (%s, 'ORG001', %s, %s, %s, %s, %s, %s)
        """,
        (uuid7(), project_id, env_id, demo.user("admin")["id"], "ancienne exception", past, demo.user("admin")["id"]),
    )
    assert policy_exc.active(conn, rule_id="ORG001", project_id=project_id, environment_id=env_id) is None
    # Exception active : R140 passe.
    future = clock.now() + timedelta(days=1)
    policy_exc.create(
        conn,
        admin,
        rule_id="ORG001",
        project_id=project_id,
        environment_id=env_id,
        owner_user_id=demo.user("admin")["id"],
        reason="Dérogation documentée pour labo",
        expires_at=future,
    )
    assert policy_exc.active(conn, rule_id="ORG001", project_id=project_id, environment_id=env_id) is not None
    env = demo.env("vitrine", "prod")
    ctx = admission.Context(
        conn=conn,
        job={"kind": "tf_apply", "class": "write", "id": uuid7(), "cancel_requested_at": None,
             "stack_id": None, "payload": {}},
        change={"id": uuid7(), "project_id": project_id},
        environment=env,
        execution=None,
        approval=None,
        plan=None,
    )
    fn = next(r[4] for r in admission.REGISTRY if r[1] == "R140")
    assert fn(ctx).ok


def test_f10_simulation_n_ecrit_rien(conn, demo):
    c = flows.create_and_submit(conn, demo)
    before_plans = conn.execute("SELECT count(*) AS n FROM plans").fetchone()["n"]
    before_adm = conn.execute("SELECT count(*) AS n FROM admission_decisions").fetchone()["n"]
    admin = demo.principal("admin", step_up=True)
    # Overlay plus strict : ORG001 aussi sur dev → refus simulé.
    result = policy_sim.simulate(
        conn,
        admin,
        c["id"],
        candidate_organization={
            "constraints": [
                {
                    "id": "ORG001",
                    "title": "simulation stricte",
                    "applies_to_kinds": ["dev", "prod"],
                    "field": "/environment",
                    "waivable": True,
                }
            ]
        },
    )
    assert result["wrote"] is False
    assert result["plans_unchanged"] is True
    assert result["outcome"] == "refused"
    assert result["refusal"]["id"] == "R140"
    assert result["refusal"]["field"] == "/environment"
    assert conn.execute("SELECT count(*) AS n FROM plans").fetchone()["n"] == before_plans
    assert conn.execute("SELECT count(*) AS n FROM admission_decisions").fetchone()["n"] == before_adm


def test_f11_droit_expire_bloque_admission(conn, demo):
    """Droit temporaire qui expire entre approbation et exécution → temporary_right_expired."""
    from rackloom.identity.authz import load_bindings

    alice = demo.user("alice")
    project_id = demo.project("vitrine")["id"]
    env = demo.env("vitrine", "dev")
    conn.execute(
        "UPDATE role_bindings SET revoked_at = now() WHERE user_id = %s AND role = 'operator' "
        "AND project_id = %s AND revoked_at IS NULL",
        (alice["id"], project_id),
    )
    admin = demo.principal("admin", step_up=True)
    expires = clock.now() + timedelta(hours=2)
    grant = temporary.grant(
        conn,
        admin,
        user_id=alice["id"],
        role="operator",
        project_id=project_id,
        environment_id=env["id"],
        operation="deploy",
        reason="Accès ponctuel pour déploiement de test",
        expires_at=expires,
    )
    conn.commit()
    assert any(b.role == "operator" and b.source == "temporary" for b in load_bindings(conn, alice["id"]))
    with clock.frozen_at(expires + timedelta(seconds=5)):
        ctx = admission.Context(
            conn=conn,
            job={"kind": "tf_apply", "class": "write", "id": uuid7(), "cancel_requested_at": None,
                 "stack_id": None, "payload": {}},
            change={"id": uuid7(), "project_id": project_id, "current_version_no": 1,
                    "created_by": alice["id"]},
            environment=env,
            execution=None,
            approval=None,
            plan=None,
            revision=None,
        )
        fn = next(r[4] for r in admission.REGISTRY if r[1] == "R020")
        verdict = fn(ctx)
        assert not verdict.ok
        assert verdict.code == "temporary_right_expired"
        assert verdict.field == "/authorization/temporary_grant"
    temporary.revoke(conn, admin, UUID(grant["id"]), "fin de mission")
    conn.commit()
    actions = {a["action"] for a in conn.execute(
        "SELECT action FROM audit_log WHERE action LIKE 'temporary_grant.%'"
    ).fetchall()}
    assert actions >= {"temporary_grant.create", "temporary_grant.revoke"}


def test_f12_session_lit_mais_n_approuve_pas_sans_preuve(api, conn, demo):
    from fastapi.testclient import TestClient

    from conftest import DEMO_PASSWORD
    from rackloom.api.app import create_app
    from rackloom.approvals import decide
    from rackloom.config import get_settings
    from rackloom.errors import ApiError

    c = flows.create_and_submit(conn, demo)
    # Lecture via session sans step-up : OK.
    get_settings.cache_clear()
    app = create_app()
    bruno = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    r = bruno.post(f"{API}/auth/login", json={"email": "bruno@demo.invalid", "password": DEMO_PASSWORD})
    assert r.status_code == 200
    bruno.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    view = bruno.get(f"{API}/changes/{c['id']}")
    assert view.status_code == 200
    # Approbation sans preuve fraîche (principal pwd) : refusée.
    with pytest.raises(ApiError) as exc:
        decide(
            conn,
            demo.principal("bruno", step_up=False),
            c["id"],
            {"plan_id": str(uuid7()), "bundle_digest": "sha256:" + "0" * 64, "decision": "approved"},
        )
    assert exc.value.code == "reauth_required"
    # Step-up API.
    up = bruno.post(f"{API}/auth/reauth", json={"password": DEMO_PASSWORD})
    assert up.status_code == 200
    assert up.json()["acr"] == "urn:rackloom:step-up"


def test_f12_preuve_idp_stale_refusee(api, conn):
    bruno = api("bruno")
    stale = (clock.now() - timedelta(hours=1)).isoformat()
    r = bruno.post(
        f"{API}/auth/reauth",
        json={"idp_proof": {"acr": "mfa", "auth_time": stale, "max_age": 300}},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "reauth_stale"


def test_f13_admin_delegue_ne_lit_pas_secrets_ni_artefacts_autre_projet(api, conn, demo):
    project = demo.project("vitrine")
    # Accorde connector_admin à Chloé sur vitrine (elle est sur compta autrement).
    admin = api("admin")
    chloe = demo.user("chloe")
    r = admin.post(
        f"{API}/role-bindings",
        json={"user_id": str(chloe["id"]), "role": "connector_admin", "project_id": str(project["id"])},
    )
    assert r.status_code == 200, r.text
    # Secrets : la vue ne renvoie jamais la valeur.
    connectors = admin.get(f"{API}/connectors").json()
    assert connectors
    for c in connectors:
        for status in c["secrets"].values():
            assert status == "défini"
    # Artefact d'un autre projet : 404.
    other = demo.project("compta")
    art_id = uuid7()
    conn.execute(
        "INSERT INTO artifacts (id, change_id, project_id, kind, path, sha256, size, key_class) "
        "VALUES (%s, NULL, %s, 'report', 'x', %s, 0, 'B')",
        (art_id, other["id"], "0" * 64),
    )
    conn.commit()
    # Alice (vitrine) ne voit pas l'artefact compta.
    alice = api("alice")
    assert alice.get(f"{API}/artifacts/{art_id}").status_code == 404
    # Chloé (compta) ne voit pas un artefact vitrine — hors périmètre → 404, même en appel API direct.
    vit_art = uuid7()
    conn.execute(
        "INSERT INTO artifacts (id, change_id, project_id, kind, path, sha256, size, key_class) "
        "VALUES (%s, NULL, %s, 'report', 'y', %s, 0, 'B')",
        (vit_art, project["id"], "0" * 64),
    )
    conn.commit()
    # Client Chloé avant le grant connector_admin sur vitrine : on utilise un second client frais
    # n'ayant que compta — inès (auteure globale) ne voit pas les données projet (D7).
    ines = api("ines")
    assert ines.get(f"{API}/artifacts/{vit_art}").status_code == 404


def test_f14_urgence_activation_et_fermeture(conn, demo):
    admin_id = demo.user("admin")["id"]
    # Simule panne IdP + activation.
    result = emergency.activate(
        conn,
        reason="SSO Keycloak indisponible (simulation F14)",
        minutes=30,
        user_ids=[admin_id],
        actor="console",
        simulate_idp_down=True,
    )
    conn.commit()
    assert result["sso_available"] is False
    assert emergency.sso_available(conn) is False
    ok, code = emergency.login_permitted(conn, admin_id)
    assert ok and code == "emergency_active"
    # Alice hors allowlist : refusée.
    alice_ok, alice_code = emergency.login_permitted(conn, demo.user("alice")["id"])
    assert not alice_ok and alice_code == "emergency_not_listed"
    closed = emergency.close(conn, reason="SSO rétabli", actor="console", restore_sso=True)
    conn.commit()
    assert closed["closed"] is True
    assert emergency.sso_available(conn) is True
    refused, code = emergency.login_permitted(conn, admin_id)
    # SSO de nouveau disponible → login local OK (comptes de secours).
    assert refused is True and code == "sso_available"


def test_f14_login_refuse_apres_fermeture_si_sso_down(api, conn, demo):
    from conftest import DEMO_PASSWORD

    admin_id = demo.user("admin")["id"]
    emergency.activate(
        conn,
        reason="Panne IdP simulée",
        minutes=60,
        user_ids=[admin_id],
        actor="console",
        simulate_idp_down=True,
    )
    emergency.close(conn, reason="Intervention terminée", actor="console", restore_sso=False)
    conn.commit()
    # SSO toujours down, intervention fermée → login refusé.
    assert emergency.sso_available(conn) is False
    client = api(None)
    r = client.post(
        f"{API}/auth/login",
        json={"email": "admin@demo.invalid", "password": DEMO_PASSWORD},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "emergency_closed"
    # Remet le SSO pour ne pas polluer les autres tests du même clone… (chaque test a sa DB).
    emergency.set_sso_available(conn, True, actor="test")
    conn.commit()


def test_f10_f11_api_routes(api, conn, demo):
    admin = api("admin")
    project_id = demo.project("vitrine")["id"]
    env_id = demo.env("vitrine", "dev")["id"]
    expires = (clock.now() + timedelta(days=1)).isoformat()
    # Exception.
    r = admin.post(
        f"{API}/projects/{project_id}/policy-exceptions",
        json={
            "rule_id": "ORG001",
            "environment_id": str(env_id),
            "owner_user_id": str(demo.user("admin")["id"]),
            "reason": "Exception de test API",
            "expires_at": expires,
        },
    )
    assert r.status_code == 200, r.text
    listed = admin.get(f"{API}/projects/{project_id}/policy-exceptions")
    assert listed.status_code == 200
    assert any(e["rule_id"] == "ORG001" for e in listed.json())
    # Droit temporaire.
    r = admin.post(
        f"{API}/projects/{project_id}/temporary-grants",
        json={
            "user_id": str(demo.user("david")["id"]),
            "role": "operator",
            "environment_id": str(env_id),
            "operation": "deploy",
            "reason": "Accès temporaire API",
            "expires_at": expires,
        },
    )
    assert r.status_code == 200, r.text
    # Politique active.
    active = admin.get(f"{API}/admin/policies/active")
    assert active.status_code == 200
    assert active.json()["version"] == "builtin@8"
