"""F20 : inventaire logiciel exportable et fin de support.

Fixtures d'avis Ubuntu avec correctif rétroporté ; export filtré par périmètre.
Niveau de preuve : testé localement (flux éditeur réel recommandé, non fait).
"""

from __future__ import annotations

from datetime import UTC, datetime

from rackloom import hosts as hosts_mod
from rackloom import software_inventory
from rackloom.db import Jsonb
from rackloom.ids import uuid7

API = "/api/v1"


def _plant_host(conn, demo, *, name: str = "vit-dev-web-20", project_slug: str = "vitrine"):
    project = demo.project(project_slug)
    env = demo.env(project_slug, "dev")
    tpl = conn.execute(
        "SELECT id FROM template_versions WHERE slug = 'ubuntu-web' AND status = 'published' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
    alice = demo.user("alice")
    change_id, stack_id, deployment_id, host_id = uuid7(), uuid7(), uuid7(), uuid7()
    conn.execute("SET CONSTRAINTS ALL DEFERRED")
    conn.execute(
        "INSERT INTO changes (id, kind, project_id, environment_id, title, state, created_by) "
        "VALUES (%s, 'create', %s, %s, 'f20', 'succeeded', %s)",
        (change_id, project["id"], env["id"], alice["id"]),
    )
    conn.execute(
        "INSERT INTO stacks (id, slug, project_id, environment_id, backend_kind, backend_ref) "
        "VALUES (%s, %s, %s, %s, 'local_demo', 'f20')",
        (stack_id, f"f20-{stack_id.hex}", project["id"], env["id"]),
    )
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
            "ok",
            datetime.now(UTC),
            Jsonb([]),
            Jsonb([{"name": name, "size_tier": "standard", "vcpu": 2, "memory_mb": 4096, "disk_gb": 40}]),
        ),
    )
    conn.execute("UPDATE changes SET deployment_id = %s WHERE id = %s", (deployment_id, change_id))
    host = conn.execute(
        "INSERT INTO hosts (id, project_id, environment_id, deployment_id, template_version_id, change_id, "
        "stack_id, vm_stable_id, name, deployment_state) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'verified') RETURNING *",
        (
            host_id,
            project["id"],
            env["id"],
            deployment_id,
            tpl["id"],
            change_id,
            stack_id,
            f"vm-f20-{host_id.hex[:8]}",
            name,
        ),
    ).fetchone()
    return dict(host)


def _observe_packages(conn, host, packages: dict[str, str], *, os_label: str = "Ubuntu 24.04.1 LTS") -> None:
    hosts_mod.observe(conn, host, "facts", {"os": os_label, "os_codename": "noble"}, "ok", "rackloom-collect", None)
    hosts_mod.observe(conn, host, "packages", packages, "ok", "rackloom-collect", None)


def test_f20_backport_not_flagged_vulnerable(conn, demo, api):
    """openssl rétroporté (>= fixed_version) : not_affected malgré amont < 3.0.13."""
    host = _plant_host(conn, demo)
    _observe_packages(
        conn,
        host,
        {
            "openssl": "3.0.2-0ubuntu1.18",
            "curl": "8.5.0-2ubuntu10.1",  # < fixed → vulnerable
            "nginx": "1.24.0-2ubuntu7",
        },
    )
    conn.commit()

    imported = api("admin").post(f"{API}/admin/software-advisories/import", json={"fixture": True})
    assert imported.status_code == 200, imported.text
    assert imported.json()["advisory_count"] == 3
    assert imported.json()["provenance"]["simulated"] is True

    inv = api("alice").get(f"{API}/projects/vitrine/software-inventory")
    assert inv.status_code == 200, inv.text
    body = inv.json()
    assert body["provenance"]["never_upstream_alone"] is True
    assert body["summary"]["feed_imported"] is True

    by_pkg = {p["name"]: p for h in body["hosts"] for p in h["packages"]}
    openssl = by_pkg["openssl"]
    assert len(openssl["advisories"]) == 1
    assert openssl["advisories"][0]["status"] == "not_affected"
    assert openssl["advisories"][0]["used_upstream_alone"] is False
    assert openssl["advisories"][0]["naive_upstream_would_flag"] is True

    curl = by_pkg["curl"]
    assert curl["advisories"][0]["status"] == "vulnerable"

    nginx = by_pkg["nginx"]
    assert nginx["advisories"][0]["status"] == "unfixed"

    # Fin de support noble encore dans le support (fixture 2029).
    assert body["hosts"][0]["eol"]["status"] == "supported"
    assert body["hosts"][0]["eol"]["series"] == "noble"


def test_f20_export_filtre_par_perimetre(conn, demo, api):
    host = _plant_host(conn, demo, name="vit-only")
    _observe_packages(conn, host, {"openssl": "3.0.2-0ubuntu1.18"})
    conn.commit()
    api("admin").post(f"{API}/admin/software-advisories/import", json={})

    exported = api("alice").post(f"{API}/projects/vitrine/software-inventory/export", json={})
    assert exported.status_code == 200, exported.text
    payload = exported.json()
    assert "export_id" in payload
    assert payload["inventory"]["project_slug"] == "vitrine"
    names = {h["name"] for h in payload["inventory"]["hosts"]}
    assert "vit-only" in names

    # Chloé (Compta) : hors périmètre vitrine → 404 (pas 403).
    denied = api("chloe").post(f"{API}/projects/vitrine/software-inventory/export", json={})
    assert denied.status_code == 404
    denied_get = api("chloe").get(f"{API}/projects/vitrine/software-inventory")
    assert denied_get.status_code == 404


def test_f20_uncertain_without_packages(conn, demo, api):
    _plant_host(conn, demo, name="vit-empty")
    conn.commit()
    api("admin").post(f"{API}/admin/software-advisories/import", json={})
    inv = api("alice").get(f"{API}/projects/vitrine/software-inventory").json()
    empty = next(h for h in inv["hosts"] if h["name"] == "vit-empty")
    assert empty["packages_observed"] is False
    assert empty["observation"]["status"] == "missing"
    assert inv["summary"]["uncertain_host_count"] >= 1


def test_f20_evaluate_pure_no_upstream_alone():
    v = software_inventory.evaluate_advisory(
        installed_version="1.2.3-1ubuntu2.1",
        fixed_version="1.2.3-1ubuntu2.1",
        upstream_fixed_version="1.3.0",
    )
    assert v["status"] == "not_affected"
    assert v["used_upstream_alone"] is False
    assert v["naive_upstream_would_flag"] is True
