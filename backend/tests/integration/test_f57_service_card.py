"""F57 : fiche « Votre service est prêt » — quatre états, allowlist, périmètre.

Niveau de preuve : testé localement (API + PostgreSQL). Les contrôles de santé sont
plantés comme le worker les aurait écrits ; T41 couvre déjà le calcul réel en labo.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rackloom.ids import uuid7

API = "/api/v1"
CANARY = "rl-canary-secret-f57-test"


def _plant_deployment(conn, demo, *, status: str, reason: str, checks: list, instances: list | None = None):
    project = demo.project("vitrine")
    env = demo.env("vitrine", "dev")
    tpl = conn.execute("SELECT id FROM template_versions WHERE slug = 'ubuntu-web' AND version = '1.0.0'").fetchone()
    alice = demo.user("alice")
    change_id, stack_id, deployment_id = uuid7(), uuid7(), uuid7()
    conn.execute("SET CONSTRAINTS ALL DEFERRED")
    conn.execute(
        "INSERT INTO changes (id, project_id, environment_id, title, state, created_by) "
        "VALUES (%s, %s, %s, 'f57', 'succeeded', %s)",
        (change_id, project["id"], env["id"], alice["id"]),
    )
    conn.execute(
        "INSERT INTO stacks (id, slug, project_id, environment_id, backend_kind, backend_ref) "
        "VALUES (%s, %s, %s, %s, 'local_demo', 'f57')",
        (stack_id, f"f57-{deployment_id.hex[:8]}", project["id"], env["id"]),
    )
    from rackloom.db import Jsonb

    inst = instances or [{"name": "vit-dev-web-01", "size_tier": "test_leger", "vcpu": 2, "memory_mb": 2048}]
    conn.execute(
        "INSERT INTO deployments (id, project_id, environment_id, template_slug, template_version_id, "
        "stack_id, created_by_change_id, owner_id, status, service_status, service_reason, "
        "service_checked_at, service_checks, instances) "
        "VALUES (%s, %s, %s, 'ubuntu-web', %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s)",
        (
            deployment_id,
            project["id"],
            env["id"],
            tpl["id"],
            stack_id,
            change_id,
            alice["id"],
            status,
            reason,
            datetime.now(UTC),
            Jsonb(checks),
            Jsonb(inst),
        ),
    )
    conn.commit()
    return deployment_id


def test_fiche_pret_avec_preuves(api, conn, demo):
    dep_id = _plant_deployment(
        conn,
        demo,
        status="ready",
        reason="tous les contrôles de santé ont réussi",
        checks=[
            {
                "host": "vit-dev-web-01",
                "state": "checked",
                "checks": [
                    {"type": "package_installed", "ok": True, "evidence": "nginx 1.24"},
                    {"type": "http_probe", "ok": True, "evidence": "HTTP 200"},
                ],
            }
        ],
    )
    alice = api("alice")
    card = alice.get(f"{API}/deployments/{dep_id}/service-card").json()
    assert card["status"] == "ready"
    assert card["headline"] == "Votre service est prêt"
    assert card["service"]["title"] == "Serveur web Ubuntu"
    assert card["environment"]["slug"] == "dev"
    assert card["owner"].startswith("Alice")
    assert "HTTP" in card["access_mode"]
    assert any(o["key"] == "name" and o["value"] == "vit-dev-web-01" for o in card["outputs"])
    assert len(card["checks"]["succeeded"]) == 2
    assert not card["checks"]["failed"]
    assert card["checked_at"]
    assert CANARY not in str(card)


def test_fiche_cree_pas_pret_sonde(api, conn, demo):
    dep_id = _plant_deployment(
        conn,
        demo,
        status="created_not_ready",
        reason="VM créée, sonde HTTP en échec",
        checks=[
            {
                "host": "vit-dev-web-01",
                "state": "checked",
                "checks": [
                    {"type": "package_installed", "ok": True, "evidence": "nginx"},
                    {
                        "type": "http_probe",
                        "ok": False,
                        "evidence": "connection refused",
                        "failure": "sonde HTTP en échec",
                    },
                ],
            }
        ],
    )
    card = api("alice").get(f"{API}/deployments/{dep_id}/service-card").json()
    assert card["status"] == "created_not_ready"
    assert "pas prêt" in card["headline"].lower() or "pas prêt" in card["detail"].lower()
    assert "sonde HTTP" in card["detail"]
    assert card["checks"]["failed"]
    assert any("sonde" in (c.get("failure") or "").lower() for c in card["checks"]["failed"])


def test_fiche_verification_indisponible(api, conn, demo):
    dep_id = _plant_deployment(
        conn,
        demo,
        status="verification_unavailable",
        reason="vérification indisponible",
        checks=[{"host": "vit-dev-web-01", "state": "unavailable", "reason": "hôte injoignable", "checks": []}],
    )
    card = api("alice").get(f"{API}/deployments/{dep_id}/service-card").json()
    assert card["status"] == "verification_unavailable"
    assert card["headline"] == "Vérification indisponible"


def test_fiche_echec(api, conn, demo):
    dep_id = _plant_deployment(
        conn, demo, status="failed", reason="aucune ressource attendue n'a été créée", checks=[], instances=[]
    )
    card = api("alice").get(f"{API}/deployments/{dep_id}/service-card").json()
    assert card["status"] == "failed"
    assert card["headline"] == "Échec du service"


def test_fiche_autre_projet_404(api, conn, demo):
    dep_id = _plant_deployment(
        conn,
        demo,
        status="ready",
        reason="ok",
        checks=[{"host": "x", "state": "checked", "checks": [{"type": "http_probe", "ok": True, "evidence": "200"}]}],
    )
    assert api("chloe").get(f"{API}/deployments/{dep_id}/service-card").status_code == 404
    assert api("chloe").get(f"{API}/deployments/{dep_id}").status_code == 404


def test_fiche_sans_secret_ni_url(api, conn, demo):
    dep_id = _plant_deployment(
        conn,
        demo,
        status="ready",
        reason="ok",
        checks=[
            {
                "host": "vit-dev-web-01",
                "state": "checked",
                "checks": [
                    {
                        "type": "http_probe",
                        "ok": True,
                        "evidence": f"password={CANARY} https://user:pass@evil.example/x",
                    }
                ],
            }
        ],
        instances=[{"name": "vit-dev-web-01", "vm_uuid": "vm-ok", "size_tier": "test_leger"}],
    )
    card = api("alice").get(f"{API}/deployments/{dep_id}/service-card").json()
    blob = str(card)
    assert CANARY not in blob
    assert "user:pass" not in blob
    assert "evil.example" not in blob


def test_change_view_embarque_la_fiche(api, conn, demo):
    """La demande expose aussi service_card pour l'écran de suivi."""
    dep_id = _plant_deployment(
        conn,
        demo,
        status="ready",
        reason="ok",
        checks=[
            {
                "host": "vit-dev-web-01",
                "state": "checked",
                "checks": [{"type": "http_probe", "ok": True, "evidence": "200"}],
            }
        ],
    )
    change_id = conn.execute(
        "SELECT created_by_change_id FROM deployments WHERE id = %s", (dep_id,)
    ).fetchone()["created_by_change_id"]
    conn.execute("UPDATE changes SET deployment_id = %s, state = 'succeeded' WHERE id = %s", (dep_id, change_id))
    conn.commit()
    view = api("alice").get(f"{API}/changes/{change_id}").json()
    assert view["deployment"]["service_card"]["headline"] == "Votre service est prêt"
