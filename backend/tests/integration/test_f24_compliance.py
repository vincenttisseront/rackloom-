"""F24 : conformité des machines.

Écart sur observations → findings (nonconforme / non applicable / absence de données) ;
proposition de remédiation = brouillon de demande ; aucun job d'écriture.
Rudder fictif : observations explicites (RAC-36 / D2).
Niveau de preuve : testé localement.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from rackloom import hosts
from rackloom.compliance import rudder_fake
from rackloom.db import Jsonb
from rackloom.ids import uuid7

API = "/api/v1"
WRITE_KINDS = (
    "tf_apply",
    "ansible_configure",
    "vm_restore",
    "service_restart",
    "lease_retire",
    "tf_import",
)


def _plant_host(conn, demo, *, name: str = "vit-dev-web-24"):
    project = demo.project("vitrine")
    env = demo.env("vitrine", "dev")
    tpl = conn.execute(
        "SELECT id FROM template_versions WHERE slug = 'ubuntu-web' AND status = 'published' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
    alice = demo.user("alice")
    change_id, stack_id, deployment_id, host_id = uuid7(), uuid7(), uuid7(), uuid7()
    conn.execute("SET CONSTRAINTS ALL DEFERRED")
    conn.execute(
        "INSERT INTO changes (id, kind, project_id, environment_id, title, state, created_by) "
        "VALUES (%s, 'create', %s, %s, 'f24', 'succeeded', %s)",
        (change_id, project["id"], env["id"], alice["id"]),
    )
    conn.execute(
        "INSERT INTO stacks (id, slug, project_id, environment_id, backend_kind, backend_ref) "
        "VALUES (%s, %s, %s, %s, 'local_demo', 'f24')",
        (stack_id, f"f24-{deployment_id.hex[:8]}", project["id"], env["id"]),
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
            "contrôles ok",
            datetime.now(UTC),
            Jsonb([]),
            Jsonb([{"name": name, "size_tier": "test_leger", "vcpu": 2, "memory_mb": 2048}]),
        ),
    )
    conn.execute("UPDATE changes SET deployment_id = %s WHERE id = %s", (deployment_id, change_id))
    conn.execute(
        "INSERT INTO hosts (id, project_id, environment_id, deployment_id, template_version_id, "
        "change_id, stack_id, vm_stable_id, name, address, host_key_fingerprint, "
        "deployment_state, enrolled_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'configured', now())",
        (
            host_id,
            project["id"],
            env["id"],
            deployment_id,
            tpl["id"],
            change_id,
            stack_id,
            f"vm-f24-{host_id.hex[:8]}",
            name,
            "10.0.0.24",
            "SHA256:f24demo",
        ),
    )
    conn.commit()
    return host_id, deployment_id


def _write_jobs(conn, project_id) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE project_id = %s AND kind = ANY(%s)",
        (project_id, list(WRITE_KINDS)),
    ).fetchone()["n"]


def test_f24_ecart_brouillon_sans_job_ecriture(api, conn, demo):
    host_id, _dep_id = _plant_host(conn, demo)
    host = conn.execute("SELECT * FROM hosts WHERE id = %s", (host_id,)).fetchone()
    # nginx absent → nonconforme ; fail2ban absent → réglage non applicable.
    hosts.observe(conn, host, "packages", {"nginx": None}, "ok", "lab-fixture", None)
    hosts.observe(
        conn,
        host,
        "services",
        {"nginx": {"active": False, "manager": "systemd", "systemd": "inactive"}},
        "ok",
        "lab-fixture",
        None,
    )
    hosts.observe(conn, host, "settings", {"nginx.server_tokens": "on"}, "ok", "lab-fixture", None)
    conn.commit()

    alice = api("alice")
    scan = alice.post(f"{API}/hosts/{host_id}/compliance/scan", json={})
    assert scan.status_code == 200, scan.text
    body = scan.json()
    assert body["correction"] is False
    assert body["status"] in ("noncompliant", "partial")
    statuses = {f["rule_id"]: f["status"] for f in body["findings"]}
    assert statuses["pkg.nginx"] == "noncompliant"
    assert statuses["svc.nginx"] == "noncompliant"
    # nginx absent → réglage nginx non applicable (pas une non-conformité de valeur).
    assert statuses["set.nginx.server_tokens"] == "not_applicable"
    assert statuses["set.fail2ban.enabled"] == "not_applicable"

    before = _write_jobs(conn, host["project_id"])
    rem = alice.post(f"{API}/hosts/{host_id}/compliance/remediate", json={})
    assert rem.status_code == 200, rem.text
    rem_body = rem.json()
    assert rem_body["change_state"] == "draft" or rem_body["change"]["state"] == "draft"
    assert rem_body["write_jobs_created"] == 0
    assert rem_body["correction"] is False
    assert _write_jobs(conn, host["project_id"]) == before
    # Job de scan = read seulement.
    job = conn.execute("SELECT kind FROM jobs WHERE id = %s", (UUID(body["job_id"]),)).fetchone()
    assert job["kind"] == "compliance_scan"


def test_f24_absence_de_donnees(api, conn, demo):
    host_id, _ = _plant_host(conn, demo, name="vit-dev-web-24b")
    alice = api("alice")
    scan = alice.post(f"{API}/hosts/{host_id}/compliance/scan", json={})
    assert scan.status_code == 200, scan.text
    body = scan.json()
    assert body["status"] == "no_data"
    assert all(f["status"] == "missing_data" for f in body["findings"])
    rem = alice.post(f"{API}/hosts/{host_id}/compliance/remediate", json={})
    assert rem.status_code == 409
    assert rem.json()["code"] == "no_gap"


def test_f24_rudder_observations_fictives(api, conn, demo):
    host_id, _ = _plant_host(conn, demo, name="vit-dev-web-24r")
    alice = api("alice")
    imported = alice.post(f"{API}/hosts/{host_id}/rudder/import-observations", json={})
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["simulated"] is True
    assert body["source"] == "rudder_fake"
    assert "packages" in body["kinds"]
    assert "settings" in body["kinds"]

    scan = alice.post(
        f"{API}/hosts/{host_id}/compliance/scan",
        json={"source": "rudder_fake"},
    )
    assert scan.status_code == 200, scan.text
    result = scan.json()
    assert result["source"] == "rudder_fake"
    by_id = {f["rule_id"]: f for f in result["findings"]}
    assert by_id["pkg.nginx"]["status"] == "compliant"
    assert by_id["set.nginx.server_tokens"]["status"] == "noncompliant"
    assert by_id["set.nginx.server_tokens"]["observed"] == "on"
    assert by_id["set.fail2ban.enabled"]["status"] == "not_applicable"


def test_f24_hors_perimetre_404(api, conn, demo):
    host_id, _ = _plant_host(conn, demo, name="vit-dev-web-24x")
    chloe = api("chloe")
    assert chloe.get(f"{API}/hosts/{host_id}/compliance").status_code == 404
    assert chloe.post(f"{API}/hosts/{host_id}/compliance/scan", json={}).status_code == 404


def test_f24_profils_publies(api, conn, demo):
    alice = api("alice")
    profiles = alice.get(f"{API}/compliance/profiles")
    assert profiles.status_code == 200
    slugs = {p["slug"] for p in profiles.json()}
    assert "ubuntu-web-baseline" in slugs
    assert all(p["status"] == "published" for p in profiles.json())


def test_f24_connecteur_rudder_demo(conn, demo):
    row = conn.execute("SELECT * FROM connectors WHERE slug = 'rudder-fake'").fetchone()
    assert row is not None
    assert row["kind"] == "rudder"
    assert row["mode"] == "simulated"
    caps = rudder_fake.capabilities()
    assert caps["inventory"]["available"] is True
    assert caps["execute"]["available"] is False
