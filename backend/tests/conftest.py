"""Fixtures communes : base PostgreSQL éphémère migrée et peuplée avec le profil de démo.

Une base modèle est créée une fois par session (migration + `demo seed`), puis clonée
(`CREATE DATABASE … TEMPLATE`) pour chaque test qui demande `db`. Aucune donnée réelle.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get("RACKLOOM_TEST_ADMIN_URL", "postgresql:///postgres")
DEMO_PASSWORD = "mot-de-passe-de-test-" + "x" * 4

DEMO_PASSWORDS = {f"{u}@demo.invalid": DEMO_PASSWORD for u in ("admin", "pub", "alice", "bruno", "chloe", "david")}


def _db_url(name: str) -> str:
    base = ADMIN_URL.rsplit("/", 1)[0]
    return f"{base}/{name}"


def _admin() -> psycopg.Connection:
    return psycopg.connect(ADMIN_URL, autocommit=True)


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    name = f"rackloom_tpl_{secrets.token_hex(4)}"
    data_dir = tmp_path_factory.mktemp("data")
    os.environ.update({
        "RACKLOOM_PROFILE": "demo",
        "RACKLOOM_DATA_DIR": str(data_dir),
        "RACKLOOM_CATALOG_DIR": str(ROOT / "catalog"),
        "RACKLOOM_FIXTURES_DIR": str(ROOT / "lab" / "fixtures"),
        "RACKLOOM_COOKIE_SECURE": "true",
    })
    from rackloom.config import get_settings

    get_settings.cache_clear()
    with _admin() as admin:
        for role in ("rackloom_api", "rackloom_worker"):
            if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = _db_url(name)
    from rackloom import db as dbmod
    from rackloom import demo

    dbmod.migrate(url)
    with psycopg.connect(url, row_factory=dict_row) as conn:
        demo.seed(conn, ROOT, passwords=DEMO_PASSWORDS)
        conn.commit()
    yield name
    with _admin() as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def db_url(template_db: str) -> Iterator[str]:
    name = f"rackloom_t_{secrets.token_hex(4)}"
    with _admin() as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(name),
                                                                       sql.Identifier(template_db)))
    url = _db_url(name)
    os.environ["RACKLOOM_DATABASE_URL"] = url
    os.environ["RACKLOOM_MIGRATOR_URL"] = url
    from rackloom import db as dbmod
    from rackloom.config import get_settings

    get_settings.cache_clear()
    dbmod.close_pool()
    yield url
    dbmod.close_pool()
    with _admin() as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, row_factory=dict_row) as c:
        yield c


class Demo:
    """Accès pratique aux identifiants du profil de démo."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def project(self, slug: str) -> dict[str, Any]:
        return self.conn.execute("SELECT * FROM projects WHERE slug = %s", (slug,)).fetchone()

    def env(self, project: str, env: str) -> dict[str, Any]:
        return self.conn.execute(
            "SELECT e.* FROM environments e JOIN projects p ON p.id = e.project_id WHERE p.slug = %s AND e.slug = %s",
            (project, env)).fetchone()

    def user(self, name: str) -> dict[str, Any]:
        return self.conn.execute("SELECT * FROM users WHERE email = %s", (f"{name}@demo.invalid",)).fetchone()

    def principal(self, name: str):
        from datetime import UTC, datetime
        from uuid import uuid4

        from rackloom.identity.authz import Principal, load_bindings

        u = self.user(name)
        return Principal(user_id=u["id"], email=u["email"], display_name=u["display_name"], session_id=uuid4(),
                         auth_method="password", auth_time=datetime.now(UTC),
                         bindings=load_bindings(self.conn, u["id"]))


@pytest.fixture
def demo(conn: psycopg.Connection) -> Demo:
    return Demo(conn)


@pytest.fixture
def api(db_url: str, monkeypatch: pytest.MonkeyPatch):
    """Fabrique de clients HTTPS de test, un par personne (jar de cookies séparé), CSRF positionné."""
    from fastapi.testclient import TestClient

    from rackloom.api.app import create_app
    from rackloom.config import get_settings

    monkeypatch.setenv("RACKLOOM_PUBLIC_ORIGIN", "https://testserver")
    monkeypatch.setenv("RACKLOOM_SSE_KEEPALIVE_SECONDS", "1")
    monkeypatch.setenv("RACKLOOM_SSE_MAX_STREAM_SECONDS", "2")
    get_settings.cache_clear()
    app = create_app()

    def client(name: str | None = None) -> TestClient:
        c = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
        if name is not None:
            r = c.post("/api/v1/auth/login", json={"email": f"{name}@demo.invalid", "password": DEMO_PASSWORD})
            assert r.status_code == 200, r.text
            c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        return c

    return client


@pytest.fixture
def worker_env(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Worker en processus : clés de test éphémères, launcher « process », Terraform local."""
    from rackloom import db as dbmod
    from rackloom import security
    from rackloom.config import get_settings

    keys = tmp_path / "keys"
    security.generate_keys(keys)
    data = tmp_path / "data"
    cache = Path.home() / ".cache" / "rackloom" / "tf-plugins"
    cache.mkdir(parents=True, exist_ok=True)
    for key, value in {
        "RACKLOOM_KEYS_DIR": str(keys), "RACKLOOM_DATA_DIR": str(data), "RACKLOOM_LAUNCHER_MODE": "inprocess",
        "RACKLOOM_CATALOG_DIR": str(ROOT / "catalog"), "RACKLOOM_JOB_HEARTBEAT_SECONDS": "1",
        "RACKLOOM_JOB_LEASE_SECONDS": "10", "RACKLOOM_PROCESS_TF_PLUGIN_CACHE_DIR": str(cache),
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    dbmod.close_pool()
    return data


@pytest.fixture
def worker(worker_env: Path):
    from rackloom.worker.main import Worker

    w = Worker(worker_id="test-worker-1")
    w.register()
    return w
