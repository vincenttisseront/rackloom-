"""F40 : restauration VM en réseau isolé (quarantaine).

Résolution → quarantaine uniquement ; nom distinct ; pas de rattachement prod.
Exécution simulée (D2) : pas de restauration réelle.
Niveau de preuve : testé localement.
"""

from __future__ import annotations

from rackloom import backups, restore
from rackloom.changes import service
from rackloom.db import Jsonb
from rackloom.ids import uuid7
from rackloom.policy import BUILTIN_VERSION


def _active_deployment(conn, demo):
    """Déploiement actif minimal pour exercer la restauration."""
    project = demo.project("vitrine")
    env = demo.env("vitrine", "dev")
    alice = demo.user("alice")["id"]
    tpl = conn.execute(
        "SELECT id, slug FROM template_versions WHERE slug = 'ubuntu-web' AND status = 'published' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
    stack_id = uuid7()
    dep_id = uuid7()
    seed_change = uuid7()
    conn.execute(
        "INSERT INTO changes (id, kind, project_id, environment_id, title, state, created_by) "
        "VALUES (%s, 'create', %s, %s, 'seed F40', 'succeeded', %s)",
        (seed_change, project["id"], env["id"], alice),
    )
    conn.execute(
        "INSERT INTO stacks (id, slug, project_id, environment_id, backend_kind, backend_ref) "
        "VALUES (%s, %s, %s, %s, 'local_demo', %s)",
        (stack_id, str(dep_id), project["id"], env["id"], f"local://{dep_id}"),
    )
    instances = [{"name": "vit-dev-web-01", "existing": True}]
    conn.execute(
        "INSERT INTO deployments (id, project_id, environment_id, template_slug, template_version_id, "
        "stack_id, created_by_change_id, status, instances, service_status, owner_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, 'ready', %s)",
        (
            dep_id,
            project["id"],
            env["id"],
            tpl["slug"],
            tpl["id"],
            stack_id,
            seed_change,
            Jsonb(instances),
            alice,
        ),
    )
    conn.execute("UPDATE changes SET deployment_id = %s WHERE id = %s", (dep_id, seed_change))
    conn.commit()
    return conn.execute("SELECT * FROM deployments WHERE id = %s", (dep_id,)).fetchone()


def test_f40_resolution_quarantaine_et_nom_distinct(conn, demo):
    assert BUILTIN_VERSION == "builtin@9"
    dep = _active_deployment(conn, demo)
    # Preuve de sauvegarde pour R115 (restauration sensible).
    backups.set_proof(
        conn,
        "vit-dev-web-01",
        status="ok",
        proof_kind=backups.PROOF_APPROVED,
    )
    conn.commit()
    p = demo.principal("alice")
    change = restore.create_restore_change(conn, p, dep["id"], source_instance="vit-dev-web-01")
    assert change["kind"] == "restore"
    rev = service.current_revision(conn, change)
    assert rev["outcome"] == "resolved", rev["block_reasons"]
    resolved = rev["resolved"]
    assert resolved["placement"]["network"]["role"] == "quarantine"
    assert resolved["restore"]["attach_production_network"] is False
    assert resolved["restore"]["source_instance"] == "vit-dev-web-01"
    names = [i["name"] for i in resolved["instances"]]
    assert "vit-dev-web-01" not in names
    assert all("rst" in n for n in names)
    restore.verify_plan(resolved, "vit-dev-web-01")


def test_f40_refuse_reseau_production(conn, demo):
    """Si le réseau résolu n'est pas quarantaine → erreur."""
    try:
        restore.assert_isolated_network({"stable_id": "network-301", "role": "app"})
        raise AssertionError("aurait dû refuser")
    except Exception as exc:
        assert getattr(exc, "code", None) == "restore_network_not_quarantine"


def test_f40_parcours_simule(conn, demo, worker):
    """Soumission → plan simulé → approbation → vm_restore simulé."""
    from rackloom import approvals

    dep = _active_deployment(conn, demo)
    backups.set_proof(conn, "vit-dev-web-01", status="ok", proof_kind=backups.PROOF_APPROVED)
    conn.commit()
    alice = demo.principal("alice")
    change = restore.create_restore_change(conn, alice, dep["id"], source_instance="vit-dev-web-01")
    conn.commit()
    submitted = service.submit(conn, alice, change["id"], change["current_revision_no"])
    assert submitted["change"]["state"] == "plan_ready"
    assert submitted.get("plan_id")
    conn.commit()

    plan = conn.execute(
        "SELECT * FROM plans WHERE change_id = %s ORDER BY created_at DESC LIMIT 1", (change["id"],)
    ).fetchone()
    bruno = demo.principal("bruno", step_up=True)
    res = approvals.decide(
        conn,
        bruno,
        change["id"],
        {
            "decision": "approved",
            "plan_id": str(plan["id"]),
            "bundle_digest": plan["bundle_digest"],
        },
    )
    assert res["job_id"]
    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (res["job_id"],)).fetchone()
    assert job["kind"] == "vm_restore"
    conn.commit()

    worker.drain(kinds=["vm_restore"])
    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (res["job_id"],)).fetchone()
    assert job["state"] == "succeeded"
    change = conn.execute("SELECT * FROM changes WHERE id = %s", (change["id"],)).fetchone()
    assert change["state"] == "succeeded"
    step = conn.execute(
        "SELECT * FROM job_steps WHERE job_id = %s AND simulated ORDER BY seq DESC LIMIT 1",
        (job["id"],),
    ).fetchone()
    assert step is not None
    assert step["summary"].get("network_role") == "quarantine"
