"""F23 : passerelle de télémétrie — mTLS, isolation projet, quotas.

Niveau de preuve : testé localement (certificats clients de test sous keys_dir ;
projet usurpé refusé ; quota appliqué dans la durée). Pas de laboratoire réel.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rackloom import clock, telemetry
from rackloom.identity.authz import Principal, load_bindings

API = "/api/v1"


def _admin_principal(conn, demo) -> Principal:
    admin = demo.user("admin")
    return Principal(
        user_id=admin["id"],
        email=admin["email"],
        display_name=admin["display_name"],
        session_id=admin["id"],
        auth_method="test",
        auth_time=datetime.now(UTC),
        bindings=load_bindings(conn, admin["id"]),
    )


def _enroll(api, *, slug: str, project: str = "vitrine", name: str | None = None) -> dict:
    r = api("admin").post(
        f"{API}/admin/telemetry/agents/enroll",
        json={"slug": slug, "name": name or f"Agent {slug}", "project": project},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_f23_enroll_mtls_et_ingest_nominal(conn, demo, api, tmp_path, monkeypatch):
    """Certificat client émis ; ingest metrics/logs attribués au projet authentifié."""
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()

    body = _enroll(api, slug="vit-metrics-1")
    agent = body["agent"]
    assert agent["project_slug"] == "vitrine"
    assert agent["status"] == "enrolled"
    cert = body["client_certificate"]
    assert cert["fingerprint_sha256"] == agent["cert_fingerprint_sha256"]
    assert "BEGIN CERTIFICATE" in cert["cert_pem"]
    assert telemetry.fingerprint_pem(cert["cert_pem"]) == cert["fingerprint_sha256"]

    fp = cert["fingerprint_sha256"]
    r = api(None).post(
        f"{API}/telemetry/ingest",
        json={"kind": "metrics", "project": "vitrine", "payload": {"cpu": 0.42, "host": "lab-1"}},
        headers={"X-Client-Cert-SHA256": fp},
    )
    assert r.status_code == 200, r.text
    sample = r.json()
    assert sample["project_slug"] == "vitrine"
    assert sample["kind"] == "metrics"
    assert sample["byte_size"] > 0
    assert sample["quota"]["used_bytes"] == sample["byte_size"]

    listed = api("alice").get(f"{API}/projects/vitrine/telemetry").json()
    assert listed["limits"]["max_payload_bytes"] >= 1024
    assert len(listed["samples"]) == 1
    assert listed["samples"][0]["payload"]["cpu"] == 0.42


def test_f23_projet_usurpe_refuse(conn, demo, api, tmp_path, monkeypatch):
    """Projet déclaré différent du projet de l'agent → 403 project_spoofed."""
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()

    body = _enroll(api, slug="vit-spoof")
    fp = body["client_certificate"]["fingerprint_sha256"]

    r = api(None).post(
        f"{API}/telemetry/ingest",
        json={"kind": "logs", "project": "compta", "payload": {"line": "x"}},
        headers={"X-Client-Cert-SHA256": fp},
    )
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "project_spoofed"

    # Aucun échantillon stocké pour vitrine ni compta.
    vit = api("alice").get(f"{API}/projects/vitrine/telemetry").json()
    assert vit["samples"] == []
    other = api("chloe").get(f"{API}/projects/compta/telemetry")
    # Chloé a accès à compta ; liste vide.
    assert other.status_code == 200
    assert other.json()["samples"] == []


def test_f23_quota_applique_dans_la_duree(conn, demo, api, tmp_path, monkeypatch):
    """Quota par fenêtre : dépassement durable → 429 ; nouvelle fenêtre → accepté."""
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()

    t0 = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    # Enrôlement sous horloge figée pour que not_before ≤ t0.
    with clock.frozen_at(t0):
        body = _enroll(api, slug="vit-quota")
    fp = body["client_certificate"]["fingerprint_sha256"]
    project_id = demo.project("vitrine")["id"]

    # Fenêtre courte, quota serré pour forcer le refus.
    actor = _admin_principal(conn, demo)
    telemetry.set_limits(
        conn,
        actor,
        project_id,
        max_payload_bytes=4096,
        quota_bytes_per_window=100,
        window_seconds=3600,
        retention_days=1,
    )
    conn.commit()

    with clock.frozen_at(t0):
        # ~86 octets : passe sous le quota de 100.
        ok = api(None).post(
            f"{API}/telemetry/ingest",
            json={"kind": "metrics", "payload": {"a": "x" * 80}},
            headers={"X-Client-Cert-SHA256": fp},
        )
        assert ok.status_code == 200, ok.text
        used = ok.json()["quota"]["used_bytes"]
        assert used <= 100

        # Second échantillon du même ordre → dépassement durable de la fenêtre.
        refused = api(None).post(
            f"{API}/telemetry/ingest",
            json={"kind": "metrics", "payload": {"a": "y" * 80}},
            headers={"X-Client-Cert-SHA256": fp},
        )
        assert refused.status_code == 429, refused.text
        assert refused.json()["code"] == "quota_exceeded"

    # Nouvelle fenêtre horaire : le quota repart.
    with clock.frozen_at(t0 + timedelta(hours=1)):
        again = api(None).post(
            f"{API}/telemetry/ingest",
            json={"kind": "logs", "payload": {"line": "after-window"}},
            headers={"X-Client-Cert-SHA256": fp},
        )
        assert again.status_code == 200, again.text
        assert again.json()["kind"] == "logs"


def test_f23_payload_trop_grand_et_sans_cert(conn, demo, api, tmp_path, monkeypatch):
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()

    body = _enroll(api, slug="vit-size")
    fp = body["client_certificate"]["fingerprint_sha256"]
    project_id = demo.project("vitrine")["id"]
    actor = _admin_principal(conn, demo)
    telemetry.set_limits(conn, actor, project_id, max_payload_bytes=128)
    conn.commit()

    big = api(None).post(
        f"{API}/telemetry/ingest",
        json={"kind": "logs", "payload": {"line": "z" * 200}},
        headers={"X-Client-Cert-SHA256": fp},
    )
    assert big.status_code == 413
    assert big.json()["code"] == "payload_too_large"

    no_cert = api(None).post(
        f"{API}/telemetry/ingest",
        json={"kind": "metrics", "payload": {"ok": True}},
    )
    assert no_cert.status_code == 401


def test_f23_isolation_lecture_interprojet(conn, demo, api, tmp_path, monkeypatch):
    """Lecture hors périmètre → 404 ; agent vitrine n'alimente pas compta."""
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(tmp_path / "keys"))
    from rackloom.config import get_settings

    get_settings.cache_clear()

    body = _enroll(api, slug="vit-iso")
    fp = body["client_certificate"]["fingerprint_sha256"]
    api(None).post(
        f"{API}/telemetry/ingest",
        json={"kind": "metrics", "payload": {"n": 1}},
        headers={"X-Client-Cert-SHA256": fp},
    )

    # alice (vitrine) ne voit pas compta.
    missing = api("alice").get(f"{API}/projects/compta/telemetry")
    assert missing.status_code == 404

    # Chloé (compta) voit une liste vide — pas les samples vitrine.
    compta = api("chloe").get(f"{API}/projects/compta/telemetry").json()
    assert compta["samples"] == []
