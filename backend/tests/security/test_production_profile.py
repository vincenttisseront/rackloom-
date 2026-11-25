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


def test_profil_demo_inchange(db_url):
    from fastapi.testclient import TestClient

    from rackloom.api.app import create_app

    with TestClient(create_app(), base_url="https://testserver") as client:
        assert client.get("/api/v1/health/live").status_code == 200
