"""Sécurité de l'API : cookies et CSRF (T29), cloisonnement interprojet (T09, T11, T12),
table des routes (T06, T32), jeton d'amorçage, révocation de rôle, D7.

Niveau de preuve : testé localement (FastAPI TestClient en HTTPS, PostgreSQL éphémère).
"""

from __future__ import annotations

import inspect
import re
import time
import uuid

import anyio
import pytest

import flows
from rackloom import artifacts, security
from rackloom.api import routes_admin, routes_auth, routes_changes, routes_follow, routes_projects, routes_resources
from rackloom.changes import service
from rackloom.identity.service import create_bootstrap_token

API = "/api/v1"
MODULES = (routes_auth, routes_projects, routes_changes, routes_follow, routes_resources, routes_admin)


def _routes():
    for module in MODULES:
        for route in module.router.routes:
            for method in route.methods - {"HEAD"}:
                yield method, route.path, route.endpoint, module


def _draft(conn, demo, who="alice", project="vitrine"):
    change = service.create_change(conn, demo.principal(who), demo.project(project), "Brouillon")
    res = service.add_version(conn, demo.principal(who), change["id"],
                              {"need": flows.need(), "base_version_no": 0})
    conn.commit()
    return change, res


def test_t29_cookie_csrf_origin(api):
    anon = api()
    r = anon.post(f"{API}/auth/login", json={"email": "alice@demo.invalid", "password": "faux-mot-de-passe-x"})
    assert r.status_code == 401
    r = anon.post(f"{API}/auth/login", json={"email": "alice@demo.invalid", "password": "x"},
                  headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "origin_refused"

    from conftest import DEMO_PASSWORD

    r = anon.post(f"{API}/auth/login", json={"email": "alice@demo.invalid", "password": DEMO_PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-rl_session=")
    flags = {part.strip().lower() for part in cookie.split(";")[1:]}
    assert {"httponly", "secure", "path=/", "samesite=strict"} <= flags
    assert not any(f.startswith("domain") for f in flags)
    csrf = r.json()["csrf_token"]
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"

    # Écriture sans jeton CSRF, avec un jeton faux, ou avec une Origin étrangère : 403.
    assert anon.post(f"{API}/projects/vitrine/changes", json={}).json()["code"] == "csrf_refused"
    r = anon.post(f"{API}/projects/vitrine/changes", json={}, headers={"X-CSRF-Token": "0" * 64})
    assert r.status_code == 403
    r = anon.post(f"{API}/projects/vitrine/changes", json={},
                  headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "origin_refused"
    r = anon.post(f"{API}/projects/vitrine/changes", json={},
                  headers={"X-CSRF-Token": csrf, "Origin": "https://testserver"})
    assert r.status_code == 200, r.text
    # Lecture sans session : 401.
    assert api().get(f"{API}/session").status_code == 401


def test_toutes_les_routes_d_ecriture_exigent_csrf(api):
    alice = api("alice")
    del alice.headers["X-CSRF-Token"]
    anonymous = {"/auth/login", "/auth/bootstrap"}
    checked = 0
    for method, path, _endpoint, _module in _routes():
        if method == "GET" or path in anonymous:
            continue
        url = API + re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
        r = alice.request(method, url, json={})
        assert r.status_code == 403 and r.json()["code"] == "csrf_refused", (method, path, r.text)
        checked += 1
    assert checked >= 20


def test_t06_t32_table_des_routes():
    """Seule `POST /changes/{c}/approvals` crée une approbation ; aucune route n'accepte de valeur technique."""
    creators = {(m, p) for m, p, endpoint, _ in _routes() if "approvals.decide" in inspect.getsource(endpoint)}
    assert creators == {("POST", "/changes/{change_id}/approvals")}
    # Aucune autre écriture directe des approbations, exécutions ou jobs d'écriture dans l'API.
    for module in MODULES:
        source = inspect.getsource(module)
        assert "INSERT INTO approvals" not in source and "INSERT INTO executions" not in source
        assert not re.search(r"kind=\"(tf_apply|ansible_configure|guest_ready|verify)\"", source)
    # Les seules routes qui écrivent un besoin passent par `service.add_version` (contrôle `locked_field`).
    writers = {(m, p) for m, p, endpoint, _ in _routes() if "add_version" in inspect.getsource(endpoint)}
    assert writers == {("POST", "/changes/{change_id}/versions")}


def test_e1_jeton_amorcage_usage_unique(api, conn):
    token = create_bootstrap_token(conn)
    conn.commit()
    anon = api()
    body = {"token": token, "email": "premier-admin@demo.invalid", "display_name": "Premier admin",
            "password": "mot-de-passe-long-de-test"}
    assert anon.post(f"{API}/auth/bootstrap", json=body).status_code == 200
    r = anon.post(f"{API}/auth/bootstrap", json={**body, "email": "second@demo.invalid"})
    assert r.status_code == 401
    conn.commit()
    denied = conn.execute("SELECT count(*) AS n FROM audit_log WHERE action LIKE 'auth.bootstrap%%' "
                          "AND outcome <> 'ok'").fetchone()["n"]
    assert denied >= 1


def test_e1_role_revoque_invalide_les_sessions(api, conn, demo):
    admin, bruno = api("admin"), api("bruno")
    assert bruno.get(f"{API}/session").status_code == 200
    binding = conn.execute("SELECT id FROM role_bindings WHERE user_id = %s AND role = 'approver'",
                           (demo.user("bruno")["id"],)).fetchone()
    r = admin.post(f"{API}/role-bindings/{binding['id']}/revoke")
    assert r.status_code == 200, r.text
    assert bruno.get(f"{API}/session").status_code == 401


def test_d16_un_seul_publicateur(api, demo):
    admin = api("admin")
    r = admin.post(f"{API}/role-bindings", json={"user_id": str(demo.user("alice")["id"]),
                                                 "role": "template_publisher"})
    assert r.status_code == 409 and r.json()["code"] == "single_publisher"


def test_d7_admin_et_publicateur_sans_role_projet(api, conn, demo):
    change, _ = _draft(conn, demo)
    for who in ("admin", "pub"):
        c = api(who)
        assert c.get(f"{API}/projects").json() == []
        assert c.get(f"{API}/projects/vitrine/changes").status_code == 404
        assert c.get(f"{API}/changes/{change['id']}").status_code == 404
    david = api("david")
    assert {p["slug"] for p in david.get(f"{API}/projects").json()} == {"vitrine", "compta"}
    assert david.get(f"{API}/changes/{change['id']}").status_code == 200
    # L'auditeur voit tout, mais n'a aucune action.
    assert david.get(f"{API}/changes/{change['id']}").json()["allowed_actions"] == []


def test_t09_interprojet(api, conn, demo):
    change, _ = _draft(conn, demo)
    chloe = api("chloe")
    assert [p["slug"] for p in chloe.get(f"{API}/projects").json()] == ["compta"]
    for url in ("/projects/vitrine/changes", "/projects/vitrine/services", "/projects/vitrine/deployments",
                "/projects/vitrine/hosts", "/projects/vitrine/environments", f"/changes/{change['id']}",
                f"/changes/{change['id']}/history", f"/changes/{change['id']}/events"):
        r = chloe.get(API + url)
        assert r.status_code == 404, (url, r.text)
        assert "vitrine" not in r.text.lower() or url.startswith("/projects/vitrine")
    # Écriture sur un changement d'un autre projet : 404 aussi, pas 403.
    r = chloe.post(f"{API}/changes/{change['id']}/submit", json={"revision_no": 1})
    assert r.status_code == 404
    services = chloe.get(f"{API}/projects/compta/services").json()
    assert services and all("vitrine" not in str(s) for s in services)


def test_t11_artefacts(api, conn, demo, worker_env):
    vitrine = demo.project("vitrine")["id"]
    report = artifacts.store(conn, project_id=vitrine, change_id=None, kind="report", key_class="B",
                             plaintext=b"rapport filtre")
    secret_plan = artifacts.store(conn, project_id=vitrine, change_id=None, kind="plan", key_class="A",
                                  plaintext=b"PK\x03\x04binaire")
    conn.commit()
    alice, chloe = api("alice"), api("chloe")
    r = alice.get(f"{API}/artifacts/{report['id']}")
    assert r.status_code == 200 and r.content == b"rapport filtre"
    assert chloe.get(f"{API}/artifacts/{report['id']}").status_code == 404
    for c in (alice, chloe, api("david")):
        assert c.get(f"{API}/artifacts/{secret_plan['id']}").status_code == 404
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'artifact.download'").fetchone()["n"] == 1
    assert security  # clés de test générées par worker_env


def _events(text: str) -> list[dict]:
    import json

    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]


def test_t12_sse_interprojet(api, conn, demo):
    vit, _ = _draft(conn, demo)
    cpt, _ = _draft(conn, demo, who="chloe", project="compta")
    chloe = api("chloe")
    assert chloe.get(f"{API}/changes/{vit['id']}/events/stream").status_code == 404
    r = chloe.get(f"{API}/projects/compta/events/stream")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    received = _events(r.text)
    assert received and {e["correlation_id"] for e in received} == {str(cpt["id"])}
    assert ": keepalive" in r.text
    assert chloe.get(f"{API}/projects/vitrine/events/stream").status_code == 404


def test_e7_reprise_last_event_id_sans_doublon(api, conn, demo):
    change, _ = _draft(conn, demo)
    alice = api("alice")
    full = _events(alice.get(f"{API}/changes/{change['id']}/events/stream").text)
    assert len(full) >= 2
    cut = full[0]["id"]
    rest = _events(alice.get(f"{API}/changes/{change['id']}/events/stream",
                             headers={"Last-Event-ID": str(cut)}).text)
    assert [e["id"] for e in rest] == [e["id"] for e in full[1:]]


def test_e7_session_revoquee_ferme_le_flux(api, conn, demo, monkeypatch):
    from rackloom.config import get_settings

    monkeypatch.setenv("RACKLOOM_SSE_SESSION_RECHECK_SECONDS", "1")
    monkeypatch.setenv("RACKLOOM_SSE_MAX_STREAM_SECONDS", "60")
    get_settings.cache_clear()
    api("alice")
    session_id = conn.execute("SELECT id FROM sessions WHERE user_id = %s ORDER BY created_at DESC LIMIT 1",
                              (demo.user("alice")["id"],)).fetchone()["id"]
    conn.commit()
    project_id = demo.project("vitrine")["id"]

    async def run() -> tuple[list[str], float]:
        chunks: list[str] = []
        start = time.monotonic()
        async for chunk in routes_follow._stream("project_id = %s", (project_id,), 0, session_id):
            chunks.append(chunk)
            if chunk.startswith("retry"):
                conn.execute("UPDATE sessions SET revoked_at = now(), revoked_reason = 'test' WHERE id = %s",
                             (session_id,))
                conn.commit()
            if "session.ended" in chunk:
                break
        return chunks, time.monotonic() - start

    chunks, elapsed = anyio.run(run)
    assert "session.ended" in chunks[-1]
    assert elapsed < 30


@pytest.mark.parametrize("who,status", [("chloe", 404), ("david", 403), ("bruno", 403)])
def test_allowed_actions_droit_retire_403_ou_404(api, conn, demo, who, status):
    change, res = _draft(conn, demo)
    r = api(who).post(f"{API}/changes/{change['id']}/submit", json={"revision_no": res["revision_no"]})
    assert r.status_code == status, r.text
