"""F35 : demandes depuis la CI/CD (identités de service).

Identité limitée à un projet ; rejeu avec la même clé → une seule demande ;
champ verrouillé → 422 `locked_field` ; aucune route d'approbation accessible.
Niveau de preuve : testé localement.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flows import need
from rackloom.api.app import create_app
from rackloom.ci_client import CiClient, CiError
from rackloom.identity import service_accounts

API = "/api/v1"


def _ci_user_id(conn):
    return conn.execute("SELECT id FROM users WHERE email = %s", ("ci-vitrine@demo.invalid",)).fetchone()["id"]


def _token(conn, demo) -> str:
    token = service_accounts.issue_token(
        conn, user_id=_ci_user_id(conn), project_id=demo.project("vitrine")["id"], label="test-f35"
    )
    conn.commit()
    return token


def _client(token: str, key: str) -> CiClient:
    http = TestClient(create_app(), base_url="https://testserver")
    ci = CiClient(http=http)  # type: ignore[arg-type]
    ci.use_service_token(token, idempotency_key=key)
    return ci


def test_f35_rejeu_idempotent(conn, demo, monkeypatch):
    monkeypatch.setenv("RACKLOOM_PUBLIC_ORIGIN", "https://testserver")
    from rackloom.config import get_settings

    get_settings.cache_clear()
    token = _token(conn, demo)
    ci = _client(token, "ci-f35-replay-1")
    first = ci.submit_need("vitrine", need(), title="CI replay")
    second = ci.submit_need("vitrine", need(), title="CI replay")
    assert first["change_id"] == second["change_id"]
    assert first["revision_no"] == second["revision_no"]
    n = conn.execute(
        "SELECT count(*) AS n FROM changes WHERE created_by = %s AND title = 'CI replay'",
        (_ci_user_id(conn),),
    ).fetchone()["n"]
    assert n == 1


def test_f35_locked_field(conn, demo, monkeypatch):
    monkeypatch.setenv("RACKLOOM_PUBLIC_ORIGIN", "https://testserver")
    from rackloom.config import get_settings

    get_settings.cache_clear()
    token = _token(conn, demo)
    ci = _client(token, "ci-f35-locked")
    with pytest.raises(CiError) as exc:
        ci.submit_need("vitrine", need(vcpu=2))
    assert exc.value.status == 422
    assert exc.value.body["code"] == "locked_field"


def test_f35_approbation_et_hors_projet(conn, demo, monkeypatch):
    monkeypatch.setenv("RACKLOOM_PUBLIC_ORIGIN", "https://testserver")
    from rackloom.config import get_settings

    get_settings.cache_clear()
    token = _token(conn, demo)
    http = TestClient(create_app(), base_url="https://testserver")
    from rackloom.changes import service

    p = demo.principal("alice")
    change = service.create_change(conn, p, demo.project("vitrine"), "F35 approve")
    service.add_version(conn, p, change["id"], {"need": need(), "base_version_no": 0})
    conn.commit()
    r = http.post(
        f"{API}/changes/{change['id']}/approvals",
        json={"plan_id": str(change["id"]), "bundle_digest": "x", "decision": "approved"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "ci-f35-approve"},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "service_identity_forbidden"
    r2 = http.post(
        f"{API}/ci/submissions",
        json={"project": "compta", "title": "x", "need": need()},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "ci-f35-compta"},
    )
    assert r2.status_code in (403, 404)


def test_f35_humain_refuse_sur_route_ci(api):
    alice = api("alice")
    r = alice.post(
        f"{API}/ci/submissions",
        json={"project": "vitrine", "title": "nope", "need": need()},
        headers={"Idempotency-Key": "ci-f35-human"},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "service_identity_required"
