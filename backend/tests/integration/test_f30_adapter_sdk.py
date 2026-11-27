"""F30 : SDK d'adaptateurs — provenance, approbation, installation, périmètre.

Recette : installation sans approbation → adapter_not_approved ;
digest inconnu → adapter_untrusted ; suite de contrat verte ;
adaptateur de référence configurable après install.
"""

from __future__ import annotations

from uuid import UUID

API = "/api/v1"


def _grant_platform_admin(conn, email: str) -> None:
    user = conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
    assert user
    existing = conn.execute(
        "SELECT id FROM role_bindings WHERE user_id = %s AND role = 'platform_admin' AND project_id IS NULL",
        (user["id"],),
    ).fetchone()
    if existing is None:
        from rackloom.ids import uuid7

        conn.execute(
            "INSERT INTO role_bindings (id, user_id, role, project_id, environment_id) "
            "VALUES (%s, %s, 'platform_admin', NULL, NULL)",
            (uuid7(), user["id"]),
        )
    conn.commit()


def test_f30_install_refusee_sans_approbation(api, conn, demo):
    admin = api("admin")
    submitted = admin.post(f"{API}/admin/adapters/submit", json={"demo_reference": True})
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["status"] == "submitted"
    assert body["slug"] == "reference-lab"
    package_id = body["id"]

    # Installer avant approbation → refus.
    refused = admin.post(f"{API}/admin/adapters/{package_id}/install")
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "adapter_not_approved"

    # Auto-approbation refusée.
    self_approve = admin.post(f"{API}/admin/adapters/{package_id}/approve", json={"reason": "moi-même"})
    assert self_approve.status_code == 403, self_approve.text
    assert self_approve.json()["code"] == "adapter_self_approval"

    # Second admin (pub promu) approuve puis admin installe.
    _grant_platform_admin(conn, "pub@demo.invalid")
    pub = api("pub")
    approved = pub.post(f"{API}/admin/adapters/{package_id}/approve", json={"reason": "revue F30"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    installed = admin.post(f"{API}/admin/adapters/{package_id}/install")
    assert installed.status_code == 200, installed.text
    assert installed.json()["status"] == "installed"
    connector_id = installed.json()["installed_connector_id"]
    assert connector_id

    # Connecteur présent avec capacités de référence.
    listed = admin.get(f"{API}/connectors")
    assert listed.status_code == 200
    ref = next(c for c in listed.json() if c["slug"] == "reference-lab")
    assert ref["kind"] == "reference"
    assert ref["capabilities"]["configure"]["available"] is True
    assert ref["capabilities"]["inventory"]["available"] is False

    # Config + test (endpoint approuvé local).
    cfg = admin.put(
        f"{API}/connectors/{connector_id}",
        json={"config": {"url": "https://reference.demo.invalid", "label": "lab F30"}},
    )
    assert cfg.status_code == 200, cfg.text
    test = admin.post(f"{API}/connectors/{connector_id}/test")
    assert test.status_code == 200, test.text
    assert test.json()["status"] == "ok"

    # Audit des étapes.
    audits = {
        r["action"]
        for r in conn.execute(
            "SELECT action FROM audit_log WHERE action LIKE 'adapter.%' ORDER BY id"
        ).fetchall()
    }
    assert {"adapter.submit", "adapter.approve", "adapter.install"} <= audits

    # Hors périmètre : opérateur ne liste pas les paquets.
    alice = api("alice")
    denied = alice.get(f"{API}/admin/adapters")
    assert denied.status_code == 403


def test_f30_digest_inconnu_refuse(api, conn, demo):
    admin = api("admin")
    bad = admin.post(
        f"{API}/admin/adapters/submit",
        json={
            "manifest": {
                "format": "rackloom.adapter_package.v1",
                "slug": "rogue-box",
                "version": "1.0.0",
                "title": "Adaptateur non signé",
                "kind": "proxmox",
                "capabilities": {"configure": True},
                "network": {"schemes": ["https"], "require_hostname_verify": True, "allow_user_supplied_url": False},
            }
        },
    )
    assert bad.status_code == 422, bad.text
    assert bad.json()["code"] == "adapter_untrusted"


def test_f30_url_libre_dans_manifeste_refusee(api, conn, demo):
    admin = api("admin")
    bad = admin.post(
        f"{API}/admin/adapters/submit",
        json={
            "manifest": {
                "format": "rackloom.adapter_package.v1",
                "slug": "bad-url",
                "version": "1.0.0",
                "title": "SSRF",
                "kind": "other",
                "capabilities": {"configure": True},
                "network": {"allow_user_supplied_url": True, "require_hostname_verify": True},
            }
        },
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "invalid_manifest"


def test_f30_suite_contrat_via_api(api, conn, demo):
    admin = api("admin")
    report = admin.get(f"{API}/admin/adapters/contract")
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["ok"] is True
    assert {r["kind"] for r in body["reports"]} >= {"vsphere", "terraform", "ansible", "model", "reference"}
    assert "adapter_not_approved" in body["error_codes"]


def test_f30_secret_en_config_reference_refuse(api, conn, demo):
    """Même contrôle que les connecteurs existants après installation."""
    admin = api("admin")
    submitted = admin.post(f"{API}/admin/adapters/submit", json={"demo_reference": True}).json()
    _grant_platform_admin(conn, "pub@demo.invalid")
    pub = api("pub")
    pub.post(f"{API}/admin/adapters/{submitted['id']}/approve", json={"reason": "ok"})
    installed = admin.post(f"{API}/admin/adapters/{submitted['id']}/install").json()
    cid = installed["installed_connector_id"]
    refuse = admin.put(
        f"{API}/connectors/{cid}",
        json={"config": {"url": "https://reference.demo.invalid", "api_token": "leak"}},
    )
    assert refuse.status_code == 422
    assert refuse.json()["code"] == "secret_in_config"


def test_f30_package_id_invalide_404(api, conn, demo):
    admin = api("admin")
    missing = admin.get(f"{API}/admin/adapters/{UUID(int=1)}")
    assert missing.status_code == 404
