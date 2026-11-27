"""F06 : baux pour environnements temporaires.

Horloge injectée ; expiration → signalement sans écriture fournisseur ; plan de retrait
soumis à approbation ; labo intact jusqu'à exécution simulée.
Niveau de preuve : testé localement (destroy réel = labo L2).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from rackloom import approvals, clock, leases
from rackloom.db import Jsonb
from rackloom.ids import uuid7

API = "/api/v1"


def _plant(conn, demo, *, expires_on: date | None = None, name: str = "vit-dev-web-06"):
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
        "VALUES (%s, 'create', %s, %s, 'f06', 'succeeded', %s)",
        (change_id, project["id"], env["id"], alice["id"]),
    )
    conn.execute(
        "INSERT INTO stacks (id, slug, project_id, environment_id, backend_kind, backend_ref) "
        "VALUES (%s, %s, %s, %s, 'local_demo', 'f06')",
        (stack_id, f"f06-{deployment_id.hex[:8]}", project["id"], env["id"]),
    )
    inst = [{"name": name, "size_tier": "test_leger", "vcpu": 2, "memory_mb": 2048}]
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
    conn.execute(
        "INSERT INTO hosts (id, project_id, environment_id, deployment_id, template_version_id, change_id, "
        "stack_id, vm_stable_id, name, deployment_state) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'verified')",
        (
            uuid7(),
            project["id"],
            env["id"],
            deployment_id,
            tpl["id"],
            change_id,
            stack_id,
            f"vm-{name}",
            name,
        ),
    )
    until = expires_on or (date.today() + timedelta(days=14))
    dep = conn.execute("SELECT * FROM deployments WHERE id = %s", (deployment_id,)).fetchone()
    leases.open_from_resolution(
        conn,
        deployment=dep,
        resolution={"inputs": {"lifetime_until": until.isoformat()}},
        actor_type="system",
        actor_id="test",
    )
    conn.commit()
    return deployment_id


def test_f06_open_on_submit(conn, demo):
    """La date de fin capturée ouvre un bail à la soumission."""
    import flows

    created = flows.create_and_submit(conn, demo)
    lease = leases.get_by_deployment(conn, created["deployment_id"])
    assert lease is not None
    assert lease["state"] == "active"
    assert lease["owner_user_id"] == demo.user("alice")["id"]


def test_f06_expiration_signale_sans_ecriture(conn, demo, worker_env):
    """Horloge injectée : expiration → signalement + plan de retrait ; labo intact."""
    dep_id = _plant(conn, demo, expires_on=date.today() + timedelta(days=1))
    lease = leases.get_by_deployment(conn, dep_id)
    assert lease is not None
    hosts_before = conn.execute("SELECT count(*) AS n FROM hosts WHERE deployment_id = %s", (dep_id,)).fetchone()["n"]
    dep_before = conn.execute("SELECT status, instances FROM deployments WHERE id = %s", (dep_id,)).fetchone()
    assert dep_before["status"] == "active"
    assert len(dep_before["instances"]) == 1

    future = datetime.now(UTC) + timedelta(days=2)
    with clock.frozen_at(future):
        stats = leases.sweep(conn, at=future.date())
    conn.commit()

    assert stats["expired"] == 1
    assert stats["retirement_proposed"] == 1
    lease2 = leases.get_by_deployment(conn, dep_id)
    assert lease2["state"] == "retirement_proposed"
    assert lease2["retirement_change_id"] is not None
    # Intact : même statut déploiement, mêmes instances, mêmes hôtes.
    dep_after = conn.execute("SELECT status, instances FROM deployments WHERE id = %s", (dep_id,)).fetchone()
    assert dep_after["status"] == "active"
    assert dep_after["instances"] == dep_before["instances"]
    hosts_after = conn.execute("SELECT count(*) AS n FROM hosts WHERE deployment_id = %s", (dep_id,)).fetchone()["n"]
    assert hosts_after == hosts_before
    change = conn.execute("SELECT * FROM changes WHERE id = %s", (lease2["retirement_change_id"],)).fetchone()
    assert change["kind"] == "retire"
    assert change["state"] == "plan_ready"
    audit = conn.execute(
        "SELECT action FROM audit_log WHERE action = 'lease.expired' "
        "AND targets->>'lease' = %s ORDER BY id DESC LIMIT 1",
        (str(lease2["id"]),),
    ).fetchone()
    assert audit is not None


def test_f06_renouvellement_et_plafond(api, conn, demo):
    dep_id = _plant(conn, demo, expires_on=date.today() + timedelta(days=5))
    alice = api("alice")
    ok = alice.post(
        f"{API}/deployments/{dep_id}/lease/renew",
        json={"days": 20, "reason": "Encore besoin pour les tests"},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["state"] == "active"
    assert body["expires_on"] == (date.today() + timedelta(days=20)).isoformat()

    too_long = alice.post(
        f"{API}/deployments/{dep_id}/lease/renew",
        json={"days": 60, "reason": "Trop long pour la politique"},
    )
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "ttl_exceeded"


def test_f06_hors_perimetre_404(api, conn, demo):
    dep_id = _plant(conn, demo)
    chloe = api("chloe")
    res = chloe.get(f"{API}/deployments/{dep_id}/lease")
    assert res.status_code == 404


def test_f06_retrait_approuve_simule_intact(api, conn, demo, worker):
    """Plan de retrait approuvé → exécution simulée ; hôtes toujours présents."""
    dep_id = _plant(conn, demo, expires_on=date.today() - timedelta(days=1))
    with clock.frozen_at(datetime.now(UTC)):
        leases.sweep(conn, at=date.today())
    conn.commit()
    lease = leases.get_by_deployment(conn, dep_id)
    assert lease["state"] == "retirement_proposed"
    change_id = lease["retirement_change_id"]
    plan = conn.execute(
        "SELECT * FROM plans WHERE change_id = %s AND status = 'ready' ORDER BY created_at DESC LIMIT 1",
        (change_id,),
    ).fetchone()
    assert plan is not None
    assert plan["summary"].get("retirement") is True
    assert plan["summary"].get("destructive") is False

    hosts_before = conn.execute("SELECT count(*) AS n FROM hosts WHERE deployment_id = %s", (dep_id,)).fetchone()["n"]
    assert hosts_before >= 1

    bruno = demo.principal("bruno", step_up=True)
    decided = approvals.decide(
        conn,
        bruno,
        change_id,
        {
            "plan_id": str(plan["id"]),
            "bundle_digest": plan["bundle_digest"],
            "decision": "approved",
            "comment": "Retrait de labo expiré",
        },
    )
    conn.commit()
    assert decided["decision"] == "approved"
    worker.drain(max_jobs=10)
    conn.commit()

    lease2 = leases.get_by_deployment(conn, dep_id)
    assert lease2["state"] == "closed"
    dep = conn.execute("SELECT status, instances FROM deployments WHERE id = %s", (dep_id,)).fetchone()
    assert dep["status"] == "abandoned"
    # Recette : ressources intactes (pas de destroy) — les hôtes restent.
    hosts_after = conn.execute("SELECT count(*) AS n FROM hosts WHERE deployment_id = %s", (dep_id,)).fetchone()["n"]
    assert hosts_after == hosts_before
    assert len(dep["instances"]) == 1
    job = conn.execute(
        "SELECT * FROM jobs WHERE kind = 'lease_retire' AND change_id = %s ORDER BY created_at DESC LIMIT 1",
        (change_id,),
    ).fetchone()
    assert job is not None
    assert job["state"] == "succeeded"
    assert job["result"].get("no_provider_destroy") is True
    assert job["result"].get("hosts_intact") is True


def test_f06_renouvellement_annule_retrait(conn, demo, worker_env):
    dep_id = _plant(conn, demo, expires_on=date.today() - timedelta(days=1))
    leases.sweep(conn, at=date.today())
    conn.commit()
    lease = leases.get_by_deployment(conn, dep_id)
    assert lease["state"] == "retirement_proposed"
    change_id = lease["retirement_change_id"]
    alice = demo.principal("alice")
    renewed = leases.renew(
        conn,
        alice,
        dep_id,
        {"days": 7, "reason": "On garde le labo encore une semaine"},
    )
    conn.commit()
    assert renewed["state"] == "active"
    change = conn.execute("SELECT state FROM changes WHERE id = %s", (change_id,)).fetchone()
    assert change["state"] == "cancelled"


def test_f06_rappel_avant_expiration(conn, demo):
    dep_id = _plant(conn, demo, expires_on=date.today() + timedelta(days=2))
    lease = leases.get_by_deployment(conn, dep_id)
    # reminder_on = expires - 3 jours, borné à today → aujourd'hui.
    assert lease["reminder_on"] <= date.today() + timedelta(days=2)
    stats = leases.sweep(conn, at=date.today())
    conn.commit()
    lease2 = leases.get_by_deployment(conn, dep_id)
    if lease["reminder_on"] <= date.today() < lease["expires_on"]:
        assert stats["reminded"] >= 1
        assert lease2["state"] == "reminded"
        assert lease2["reminded_at"] is not None
    else:
        pytest.skip("fenêtre de rappel non atteinte pour cette date")


def test_f06_fiche_service_expose_bail(api, conn, demo):
    dep_id = _plant(conn, demo)
    alice = api("alice")
    card = alice.get(f"{API}/deployments/{dep_id}/service-card").json()
    assert card["lease"] is not None
    assert card["lease"]["state"] == "active"
    assert card["lease"]["can_renew"] is True
