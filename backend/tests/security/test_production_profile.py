"""Le profil `production` refuse de démarrer l'API et le worker tant qu'un connecteur simulé existe.

Définition de terminé du lot 1, point 5. Niveau de preuve : testé localement.
"""

from __future__ import annotations

import pytest

from rackloom import db as dbmod
from rackloom.config import get_settings
from rackloom.connectors import SimulatedInProduction


@pytest.fixture
def production(db_url, monkeypatch):
    monkeypatch.setenv("RACKLOOM_PROFILE", "production")
    get_settings.cache_clear()
    dbmod.close_pool()
    yield
    dbmod.close_pool()
    get_settings.cache_clear()


def test_api_refuse_de_demarrer_avec_un_connecteur_simule(production, conn):
    from fastapi.testclient import TestClient

    from rackloom.api.app import create_app

    with pytest.raises(SimulatedInProduction, match="vsphere-fake"), TestClient(create_app()):
        pass

    conn.execute("UPDATE connectors SET mode = 'real' WHERE mode = 'simulated'")
    conn.commit()
    with TestClient(create_app(), base_url="https://testserver") as client:
        assert client.get("/api/v1/health/live").status_code == 200


def test_worker_refuse_de_demarrer_avec_un_connecteur_simule(production):
    from rackloom.worker.main import Worker

    worker = Worker(launcher=object(), worker_id="t-production")  # type: ignore[arg-type]
    with pytest.raises(SimulatedInProduction, match="vsphere-fake"):
        worker.register()


def test_identites_et_donnees_de_demo_refusees(production, conn, demo):
    from pathlib import Path

    from rackloom import chat
    from rackloom import demo as demo_profile
    from rackloom.errors import ApiError
    from rackloom.identity import service as identity

    with pytest.raises(demo_profile.DemoRefused):
        demo_profile.seed(conn, Path(__file__).resolve().parents[3])
    for kwargs in ({"email": "eve@exemple.test", "is_demo": True}, {"email": "eve@demo.invalid"}):
        with pytest.raises(ApiError) as exc:
            identity.create_user(conn, display_name="Eve", password="x" * 16, **kwargs)
        assert exc.value.code == "demo_identity_refused"
    with pytest.raises(ApiError) as exc:
        chat.set_assistant(conn, demo.principal("admin"), "fake", "nominal")
    assert exc.value.code == "fake_refused"


def test_session_de_demo_refusee_en_production(api, conn, monkeypatch):
    from conftest import DEMO_PASSWORD

    alice = api("alice")
    assert alice.get("/api/v1/session").status_code == 200
    monkeypatch.setenv("RACKLOOM_PROFILE", "production")
    get_settings.cache_clear()
    try:
        assert alice.get("/api/v1/session").status_code == 401
        r = api().post("/api/v1/auth/login", json={"email": "alice@demo.invalid", "password": DEMO_PASSWORD})
        assert r.status_code == 401
    finally:
        monkeypatch.setenv("RACKLOOM_PROFILE", "demo")
        get_settings.cache_clear()


def test_profil_demo_inchange(db_url):
    from fastapi.testclient import TestClient

    from rackloom.api.app import create_app

    with TestClient(create_app(), base_url="https://testserver") as client:
        assert client.get("/api/v1/health/live").status_code == 200
