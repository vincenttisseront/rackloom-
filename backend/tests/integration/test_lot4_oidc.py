"""Lot 4 : SSO OIDC (IdP local de test), step-up F12, urgence F14 si IdP down.

Niveau de preuve : testé localement. IdP d'entreprise : non vérifié.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from fake_oidc import FakeOidcIdP
from rackloom import security
from rackloom.api.app import create_app
from rackloom.config import get_settings
from rackloom.identity import emergency, oidc, reauth
from rackloom.identity.authz import Principal, load_bindings

API = "/api/v1"
DEMO_PASSWORD = "mot-de-passe-de-test-" + "x" * 4


@pytest.fixture
def keys(tmp_path, monkeypatch):
    keys_dir = tmp_path / "keys"
    security.generate_keys(keys_dir)
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(keys_dir))
    monkeypatch.setenv("RACKLOOM_PUBLIC_ORIGIN", "https://testserver")
    get_settings.cache_clear()
    return keys_dir


@pytest.fixture
def idp():
    server = FakeOidcIdP()
    server.start()
    yield server
    server.stop()


def _enable_oidc(conn, demo, idp: FakeOidcIdP, *, groups_map: bool = True) -> None:
    admin = demo.principal("admin", step_up=True)
    oidc.configure(
        conn,
        admin,
        enabled=True,
        issuer_url=idp.base_url,
        client_id=idp.client_id,
        scopes=["openid", "profile", "email", "groups"],
        step_up_acr="urn:rackloom:step-up",
    )
    if groups_map:
        oidc.add_group_mapping(conn, admin, idp_group="rackloom-admins", role="platform_admin")
        oidc.add_group_mapping(
            conn,
            admin,
            idp_group="rackloom-approvers",
            role="approver",
            project_id=demo.project("vitrine")["id"],
        )


def _idp_to_callback(auth_url: str) -> str:
    """Appelle l'authorize de l'IdP de test ; renvoie le chemin callback Rackloom."""
    with httpx.Client(follow_redirects=False) as http:
        r = http.get(auth_url)
    assert r.status_code in (302, 303), r.text[:200]
    loc = r.headers["location"]
    parsed = urlparse(loc)
    if parsed.scheme:
        return parsed.path + ("?" + parsed.query if parsed.query else "")
    return loc


def test_oidc_login_session_rotation_and_group_mapping(api, conn, demo, keys, idp):
    _enable_oidc(conn, demo, idp)
    conn.commit()

    local = api("admin")
    assert local.get(f"{API}/session").status_code == 200
    old_cookie = {c.name: c.value for c in local.cookies.jar}

    c = TestClient(create_app(), base_url="https://testserver", raise_server_exceptions=False)
    for name, value in old_cookie.items():
        c.cookies.set(name, value)

    begin = c.get(f"{API}/auth/oidc/login", follow_redirects=False)
    assert begin.status_code == 302
    auth_url = begin.headers["location"]
    assert "code_challenge" in auth_url
    assert "code_challenge_method=S256" in auth_url

    callback_path = _idp_to_callback(auth_url)
    assert "/api/v1/auth/oidc/callback" in callback_path
    done = c.get(callback_path, follow_redirects=False)
    assert done.status_code == 302, done.text[:400]
    assert done.headers["location"] == "/"

    sess = c.get(f"{API}/session")
    assert sess.status_code == 200, sess.text
    body = sess.json()
    assert body["auth_method"] == "oidc"
    assert body["user"]["email"] == "admin@demo.invalid"
    assert "platform_admin" in {r["role"] for r in body["roles"]}

    # Ancien cookie local révoqué (rotation).
    assert local.get(f"{API}/session").status_code == 401

    rows = conn.execute(
        "SELECT action, targets FROM audit_log WHERE action IN ('auth.login', 'oidc.begin') ORDER BY at DESC LIMIT 8"
    ).fetchall()
    blob = " ".join(str(r["targets"]) for r in rows)
    assert "rackloom-oidc-lab" not in blob
    assert not re.search(r"client_secret", blob, re.I)


def test_oidc_reauth_step_up_acr_auth_time(api, conn, demo, keys, idp):
    _enable_oidc(conn, demo, idp, groups_map=False)
    conn.commit()

    c = TestClient(create_app(), base_url="https://testserver", raise_server_exceptions=False)
    begin = c.get(f"{API}/auth/oidc/login", follow_redirects=False)
    callback_path = _idp_to_callback(begin.headers["location"])
    assert c.get(callback_path, follow_redirects=False).status_code == 302
    csrf = c.get(f"{API}/session").json()["csrf_token"]

    # Preuve simulée refusée quand OIDC est activé.
    bruno = api("bruno")
    r = bruno.post(
        f"{API}/auth/reauth",
        json={"idp_proof": {"acr": "mfa", "auth_time": "2099-01-01T00:00:00+00:00", "max_age": 300}},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "use_oidc_reauth"

    # Affaiblit la session pour forcer le step-up.
    conn.execute(
        "UPDATE sessions SET acr = 'pwd', auth_method = 'oidc', "
        "auth_time = now() - interval '1 hour' WHERE revoked_at IS NULL"
    )
    conn.commit()

    up = c.post(
        f"{API}/auth/oidc/reauth",
        headers={"X-CSRF-Token": csrf, "Origin": "https://testserver"},
    )
    assert up.status_code == 200, up.text
    auth_url = up.json()["authorization_url"]
    assert "prompt=login" in auth_url
    assert "max_age=0" in auth_url
    qs = parse_qs(urlparse(auth_url).query)
    assert qs.get("acr_values") == ["urn:rackloom:step-up"]

    callback_path = _idp_to_callback(auth_url)
    assert c.get(callback_path, follow_redirects=False).status_code == 302
    after = c.get(f"{API}/session").json()
    assert after["auth_method"] == "oidc_step_up"
    assert after["acr"] == "urn:rackloom:step-up"

    row = conn.execute(
        "SELECT s.*, u.email, u.display_name FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE u.email = 'admin@demo.invalid' AND s.revoked_at IS NULL "
        "ORDER BY s.created_at DESC LIMIT 1"
    ).fetchone()
    principal = Principal(
        user_id=row["user_id"],
        email=row["email"],
        display_name=row["display_name"],
        session_id=row["id"],
        auth_method=row["auth_method"],
        auth_time=row["auth_time"],
        bindings=load_bindings(conn, row["user_id"]),
        acr=row["acr"],
        amr=list(row["amr"] or []),
    )
    reauth.require_fresh(principal, action="approuver un plan")


def test_oidc_idp_down_emergency_local_login(api, conn, demo, keys, idp):
    _enable_oidc(conn, demo, idp, groups_map=False)
    conn.commit()

    idp.stop()
    assert oidc.probe_idp(conn)["reachable"] is False

    admin_id = demo.user("admin")["id"]
    emergency.activate(
        conn,
        reason="SSO IdP local arrêté (test lot 4)",
        minutes=30,
        user_ids=[admin_id],
        actor="console",
        simulate_idp_down=True,
    )
    conn.commit()
    assert emergency.sso_available(conn) is False

    alice = api(None)
    r = alice.post(f"{API}/auth/login", json={"email": "alice@demo.invalid", "password": DEMO_PASSWORD})
    assert r.status_code == 403
    assert r.json()["code"] == "emergency_not_listed"

    admin = api(None)
    r = admin.post(f"{API}/auth/login", json={"email": "admin@demo.invalid", "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json()["auth_method"] == "emergency"

    c = TestClient(create_app(), base_url="https://testserver", raise_server_exceptions=False)
    begin = c.get(f"{API}/auth/oidc/login", follow_redirects=False)
    assert begin.status_code == 503
    assert begin.json()["code"] == "idp_unavailable"

    emergency.close(conn, reason="fin de test", actor="console", restore_sso=True)
    conn.commit()


def test_oidc_admin_config_hides_secret(conn, demo, keys, idp):
    _enable_oidc(conn, demo, idp, groups_map=False)
    admin_p = demo.principal("admin", step_up=True)
    oidc.configure(conn, admin_p, client_secret="super-secret-lab-value")
    conn.commit()
    view = oidc.admin_view(conn)
    assert view["client_secret_configured"] is True
    assert "super-secret-lab-value" not in str(view)
    assert "client_secret" not in view
