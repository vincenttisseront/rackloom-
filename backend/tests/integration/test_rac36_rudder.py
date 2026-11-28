"""RAC-36 : connecteur Rudder réel (HTTP local) → observations F24.

Niveau de preuve : testé localement. Labo Rudder réel : non vérifié (D2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from rackloom import hosts, secrets
from rackloom.connectors import capabilities_for
from rackloom.db import Jsonb
from rackloom.ids import uuid7
from support.rudder_http import rudder_https_server

API = "/api/v1"


def _plant_host(conn, demo, *, name: str = "vit-dev-web-36"):
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
        "VALUES (%s, 'create', %s, %s, 'rac36', 'succeeded', %s)",
        (change_id, project["id"], env["id"], alice["id"]),
    )
    conn.execute(
        "INSERT INTO stacks (id, slug, project_id, environment_id, backend_kind, backend_ref) "
        "VALUES (%s, %s, %s, %s, 'local_demo', 'rac36')",
        (stack_id, f"rac36-{deployment_id.hex[:8]}", project["id"], env["id"]),
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
            f"vm-rac36-{host_id.hex[:8]}",
            name,
            "10.0.0.36",
            "SHA256:rac36demo",
        ),
    )
    conn.commit()
    return host_id, deployment_id


def _insert_real_rudder(conn, *, slug: str = "rudder-lab") -> dict:
    row = conn.execute(
        "INSERT INTO connectors (id, slug, kind, mode, capabilities) VALUES (%s, %s, 'rudder', 'real', %s) RETURNING *",
        (uuid7(), slug, Jsonb(capabilities_for("rudder", "real"))),
    ).fetchone()
    conn.commit()
    return row


def test_rac36_configure_sans_reveler_secret(api, conn, demo, tmp_path: Path, worker_env):
    admin = api("admin")
    connector = _insert_real_rudder(conn)
    with rudder_https_server(tmp_path) as srv:
        cfg = admin.put(
            f"{API}/connectors/{connector['id']}",
            json={"config": {"url": srv["url"], "ca_pem": srv["ca_pem"], "default_node_id": srv["node_id"]}},
        )
        assert cfg.status_code == 200, cfg.text
        body = cfg.json()
        assert body["config"]["url"] == srv["url"]
        assert body["config"].get("ca_imported") is True
        assert "ca_pem" not in body["config"]
        assert "token" not in body["config"]
        assert body["secrets"] == {}

        secret = admin.put(
            f"{API}/connectors/{connector['id']}/secrets/token",
            json={"value": srv["token"]},
        )
        assert secret.status_code == 200, secret.text
        assert secret.json()["status"] == "défini"
        assert "value" not in secret.json()

        listed = admin.get(f"{API}/connectors")
        entry = next(c for c in listed.json() if c["id"] == str(connector["id"]))
        assert entry["secrets"]["token"] == "défini"
        assert entry["capabilities"]["execute"]["available"] is False
        assert entry["capabilities"]["inventory"]["mode"] == "real"

        # Le secret n'est jamais renvoyé ; seule la référence existe en base.
        row = conn.execute("SELECT secret_refs, config FROM connectors WHERE id = %s", (connector["id"],)).fetchone()
        assert "token" in row["secret_refs"]
        assert "token" not in (row["config"] or {})
        assert secrets.exists(conn, row["secret_refs"]["token"])


def test_rac36_test_connection_tls(api, conn, demo, tmp_path: Path, worker_env):
    admin = api("admin")
    connector = _insert_real_rudder(conn, slug="rudder-tls")
    with rudder_https_server(tmp_path) as srv:
        assert (
            admin.put(
                f"{API}/connectors/{connector['id']}",
                json={"config": {"url": srv["url"], "ca_pem": srv["ca_pem"]}},
            ).status_code
            == 200
        )
        result = admin.post(f"{API}/connectors/{connector['id']}/test")
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["status"] == "ok"
        assert body["simulated"] is False


def test_rac36_import_observations_via_worker(api, conn, demo, tmp_path: Path, worker):
    alice = api("alice")
    admin = api("admin")
    host_id, _ = _plant_host(conn, demo, name="vit-dev-web-36")
    connector = _insert_real_rudder(conn, slug="rudder-import")

    with rudder_https_server(tmp_path) as srv:
        assert (
            admin.put(
                f"{API}/connectors/{connector['id']}",
                json={
                    "config": {
                        "url": srv["url"],
                        "ca_pem": srv["ca_pem"],
                        "default_node_id": srv["node_id"],
                    }
                },
            ).status_code
            == 200
        )
        assert (
            admin.put(
                f"{API}/connectors/{connector['id']}/secrets/token",
                json={"value": srv["token"]},
            ).status_code
            == 200
        )

        queued = alice.post(
            f"{API}/hosts/{host_id}/rudder/import-observations",
            json={"connector_id": str(connector["id"]), "node_id": srv["node_id"]},
        )
        assert queued.status_code == 200, queued.text
        body = queued.json()
        assert body["queued"] is True
        assert body["simulated"] is False
        assert body["source"] == "rudder"
        job_id = UUID(body["job_id"])

        assert worker.drain(kinds=["rudder_import"]) == ["rudder_import"]
        job = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
        assert job["state"] == "succeeded"
        assert job["class"] == "read"
        assert job["result"]["simulated"] is False
        assert "packages" in job["result"]["kinds"]

        obs = hosts.latest_observations(conn, host_id)
        assert obs["packages"]["source"] == "rudder"
        assert obs["packages"]["data"]["nginx"] == "1.24.0-2ubuntu7"
        assert obs["settings"]["data"]["nginx.server_tokens"] == "on"

        scan = alice.post(
            f"{API}/hosts/{host_id}/compliance/scan",
            json={"source": "rudder"},
        )
        assert scan.status_code == 200, scan.text
        findings = {f["rule_id"]: f for f in scan.json()["findings"]}
        assert findings["pkg.nginx"]["status"] == "compliant"
        assert findings["set.nginx.server_tokens"]["status"] == "noncompliant"


def test_rac36_refuse_report_client_sur_reel(api, conn, demo, tmp_path: Path, worker_env):
    alice = api("alice")
    admin = api("admin")
    host_id, _ = _plant_host(conn, demo, name="vit-dev-web-36b")
    connector = _insert_real_rudder(conn, slug="rudder-no-report")
    with rudder_https_server(tmp_path) as srv:
        admin.put(
            f"{API}/connectors/{connector['id']}",
            json={"config": {"url": srv["url"], "ca_pem": srv["ca_pem"]}},
        )
        admin.put(
            f"{API}/connectors/{connector['id']}/secrets/token",
            json={"value": srv["token"]},
        )
        refused = alice.post(
            f"{API}/hosts/{host_id}/rudder/import-observations",
            json={
                "connector_id": str(connector["id"]),
                "node_id": srv["node_id"],
                "report": {"packages": {"nginx": "1.0"}},
            },
        )
        assert refused.status_code == 422
        assert refused.json()["code"] == "report_forbidden"


def test_rac36_fictif_toujours_disponible(api, conn, demo):
    """Le connecteur rudder-fake de démo reste utilisable (F24)."""
    host_id, _ = _plant_host(conn, demo, name="vit-dev-web-36f")
    alice = api("alice")
    imported = alice.post(f"{API}/hosts/{host_id}/rudder/import-observations", json={})
    assert imported.status_code == 200, imported.text
    assert imported.json()["simulated"] is True
    assert imported.json()["source"] == "rudder_fake"
