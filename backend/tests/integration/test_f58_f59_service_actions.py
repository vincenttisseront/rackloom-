"""F58 / F59 : actions fiche service et demande d'accès.

Niveau de preuve : testé localement (API + PostgreSQL). IdP réel : non (D2) — faux IdP.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rackloom import access
from rackloom.changes import service
from rackloom.connectors import idp_fake
from rackloom.db import Jsonb
from rackloom.deployments import actions as service_actions
from rackloom.ids import uuid7

API = "/api/v1"


@pytest.fixture(autouse=True)
def _isolate_idp():
    """Isole l'état partagé du faux IdP entre les tests."""
    idp_fake.clear_memory()
    yield
    idp_fake.clear_memory()


def _plant_ready(conn, demo, *, size_tier: str = "test_leger", name: str = "vit-dev-web-58"):
    project = demo.project("vitrine")
    env = demo.env("vitrine", "dev")
    tpl = conn.execute(
        "SELECT id FROM template_versions WHERE slug = 'ubuntu-web' AND status = 'published' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
    alice = demo.user("alice")
    change_id, stack_id, deployment_id = uuid7(), uuid7(), uuid7()
    conn.execute("SET CONSTRAINTS ALL DEFERRED")
    conn.execute(
        "INSERT INTO changes (id, kind, project_id, environment_id, title, state, created_by) "
        "VALUES (%s, 'create', %s, %s, 'f58', 'succeeded', %s)",
        (change_id, project["id"], env["id"], alice["id"]),
    )
    conn.execute(
        "INSERT INTO stacks (id, slug, project_id, environment_id, backend_kind, backend_ref) "
        "VALUES (%s, %s, %s, %s, 'local_demo', 'f58')",
        (stack_id, f"f58-{deployment_id.hex[:8]}", project["id"], env["id"]),
    )
    inst = [{"name": name, "size_tier": size_tier, "vcpu": 2, "memory_mb": 2048}]
    conn.execute(
        "INSERT INTO deployments (id, project_id, environment_id, template_slug, template_version_id, "
        "stack_id, created_by_change_id, owner_id, status, service_status, service_reason, "
        "service_checked_at, service_checks, instances) "
        "VALUES (%s, %s, %s, 'ubuntu-web', %s, %s, %s, %s, 'active', 'ready', %s, %s, %s, %s)",
        (
            deployment_id,
            project["id"],
            env["id"],
            tpl["id"],
            stack_id,
            change_id,
            alice["id"],
            "tous les contrôles de santé ont réussi",
            datetime.now(UTC),
            Jsonb(
                [
                    {
                        "host": name,
                        "state": "checked",
                        "checks": [{"type": "http_probe", "ok": True, "evidence": "HTTP 200"}],
                    }
                ]
            ),
            Jsonb(inst),
        ),
    )
    conn.execute("UPDATE changes SET deployment_id = %s WHERE id = %s", (deployment_id, change_id))
    conn.commit()
    return deployment_id


# --- F58 --------------------------------------------------------------------------------------


def test_f58_increase_capacity_ouvre_demande_avec_plan(api, conn, demo, worker):
    dep_id = _plant_ready(conn, demo, size_tier="test_leger")
    alice = api("alice")
    card = alice.get(f"{API}/deployments/{dep_id}/service-card").json()
    actions = {a["id"] for a in card["service_actions"]}
    assert "increase_capacity" in actions
    assert "request_restart" in actions
    # Impact visible (remplacement possible).
    cap = next(a for a in card["service_actions"] if a["id"] == "increase_capacity")
    assert cap["impact"] == "may_replace"
    assert cap["target_size_tier"] == "standard"

    started = alice.post(f"{API}/deployments/{dep_id}/actions/increase_capacity", json={})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["action"] == "increase_capacity"
    change = body["change"]
    assert change["kind"] == "modify"
    assert change["deployment"]["id"] == str(dep_id)
    rev = change["revision"]
    assert rev["outcome"] == "resolved"
    blob = str(rev.get("summary") or rev).lower()
    assert "standard" in blob
    summary = rev.get("summary") or {}
    assert summary.get("replaces"), "remplacement annoncé dès la révision"

    # Sans droit : 403.
    david = api("david")
    denied = david.post(f"{API}/deployments/{dep_id}/actions/increase_capacity", json={})
    assert denied.status_code in (403, 404), denied.text


def test_f58_remplacement_affiche_et_bloque(api, conn, demo):
    """Augmenter la capacité (size_tier) → remplacement annoncé (plan bloqué : T26)."""
    from rackloom.api.views import change_view

    dep_id = _plant_ready(conn, demo, size_tier="test_leger", name="vit-dev-web-58b")
    alice = demo.principal("alice")
    result = service_actions.start(conn, alice, dep_id, "increase_capacity")
    change = result["change"]
    conn.commit()
    rev = service.current_revision(conn, change)
    replaces = (rev["resolved"].get("modification") or {}).get("replaces") or []
    assert replaces, "le remplacement doit être annoncé dès la révision"
    assert result.get("impact") == "may_replace"
    view = change_view(conn, alice, change)
    assert view["revision"]["summary"].get("replaces") == replaces


def test_f58_restart_parcours_simule(api, conn, demo, worker):
    dep_id = _plant_ready(conn, demo, name="vit-dev-web-58r")
    alice = api("alice")
    started = alice.post(f"{API}/deployments/{dep_id}/actions/request_restart", json={})
    assert started.status_code == 200, started.text
    change = started.json()["change"]
    assert change["kind"] == "restart"
    assert started.json().get("impact") == "interruption"

    # Soumettre → plan simulé immédiat.
    sub = alice.post(
        f"{API}/changes/{change['id']}/submit",
        json={"revision_no": change["revision"]["revision_no"]},
    )
    assert sub.status_code == 200, sub.text
    view = alice.get(f"{API}/changes/{change['id']}").json()
    assert view["state"] == "plan_ready"
    assert view["plan"]["interruption"] is True
    assert any(r["code"] == "interruption" for r in view["plan"]["risks"])

    # Approbation Bruno → job service_restart simulé.
    bruno = api("bruno")
    plan = view["plan"]
    decision = bruno.post(
        f"{API}/changes/{change['id']}/approvals",
        json={"decision": "approved", "plan_id": plan["id"], "bundle_digest": plan["bundle_digest"]},
    )
    assert decision.status_code == 200, decision.text
    worker.drain(kinds=["service_restart"])
    final = alice.get(f"{API}/changes/{change['id']}").json()
    assert final["state"] == "succeeded"


def test_f58_sans_droit_api(api, conn, demo):
    dep_id = _plant_ready(conn, demo, name="vit-dev-web-58x")
    # Chloé (autre projet) : 404 hors périmètre.
    chloe = api("chloe")
    r = chloe.post(f"{API}/deployments/{dep_id}/actions/request_restart", json={})
    assert r.status_code == 404
    # David (auditeur) : voit peut-être le projet mais pas operator → 403.
    david = api("david")
    r2 = david.post(f"{API}/deployments/{dep_id}/actions/request_restart", json={})
    assert r2.status_code in (403, 404)


# --- F59 --------------------------------------------------------------------------------------


def test_f59_demande_acces_accorde_apres_idp(api, conn, demo):
    access.ensure_demo_mappings(conn, published_by=demo.user("admin")["id"])
    conn.commit()
    dep_id = _plant_ready(conn, demo, name="vit-dev-web-59")
    alice = api("alice")
    card = alice.get(f"{API}/deployments/{dep_id}/service-card").json()
    levels = {lvl["id"] for lvl in card["access_request"]["levels"]}
    assert "read" in levels and "operate" in levels
    assert all(lvl["eligible"] for lvl in card["access_request"]["levels"])

    # Champ group refusé.
    bad = alice.post(
        f"{API}/deployments/{dep_id}/access-requests",
        json={"access_level": "read", "group": "admins"},
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "locked_field"

    created = alice.post(
        f"{API}/deployments/{dep_id}/access-requests",
        json={"access_level": "read", "subject": "self"},
    )
    assert created.status_code == 200, created.text
    req = created.json()
    assert req["state"] == "pending_approval"
    assert req["idp_group"] is None  # pas révélé au demandeur
    assert "password" not in str(req).lower()

    # Alice ne peut pas s'auto-approuver.
    self_ok = alice.post(f"{API}/access-requests/{req['id']}/approve", json={})
    assert self_ok.status_code == 403

    bruno = api("bruno")
    granted = bruno.post(f"{API}/access-requests/{req['id']}/approve", json={})
    assert granted.status_code == 200, granted.text
    body = granted.json()
    assert body["state"] == "granted"
    assert body["status_badge"] == "accès accordé"
    assert body["granted_at"]


def test_f59_echec_sync_et_relance_idempotente(api, conn, demo):
    access.ensure_demo_mappings(conn, published_by=demo.user("admin")["id"])
    idp_fake.set_fail_next(conn, fail=True, message="annuaire indisponible (simulé)")
    conn.commit()
    dep_id = _plant_ready(conn, demo, name="vit-dev-web-59b")
    alice = api("alice")
    created = alice.post(
        f"{API}/deployments/{dep_id}/access-requests",
        json={"access_level": "operate", "subject": "self"},
    )
    assert created.status_code == 200, created.text
    req_id = created.json()["id"]

    bruno = api("bruno")
    failed = bruno.post(f"{API}/access-requests/{req_id}/approve", json={})
    assert failed.status_code == 200, failed.text
    body = failed.json()
    assert body["state"] == "sync_failed"
    assert body["status_badge"] == "échec de synchronisation"
    assert "indisponible" in (body["sync_error"] or "").lower() or body["sync_error"]

    # Relance → une seule attribution.
    retry = bruno.post(f"{API}/access-requests/{req_id}/retry-sync", json={})
    assert retry.status_code == 200, retry.text
    assert retry.json()["state"] == "granted"
    asg = conn.execute(
        "SELECT count(*) AS n FROM service_access_assignments WHERE request_id = %s", (req_id,)
    ).fetchone()
    assert asg["n"] == 1

    # Seconde relance après granted : état invalide ou rejoue sans doublon.
    again = bruno.post(f"{API}/access-requests/{req_id}/retry-sync", json={})
    assert again.status_code in (200, 409)
    asg2 = conn.execute(
        "SELECT count(*) AS n FROM service_access_assignments WHERE request_id = %s", (req_id,)
    ).fetchone()
    assert asg2["n"] == 1
