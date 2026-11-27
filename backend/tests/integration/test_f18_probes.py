"""F18 : sondes et objectifs de disponibilité.

Recette : service actif + sonde en échec depuis le segment filtrant → unavailable.
Trois dimensions distinctes : service_state, probe_health, freshness.
Niveau de preuve : testé localement (segments simulés ; segments réels = labo).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rackloom import probes
from rackloom.db import Jsonb
from rackloom.ids import uuid7
from rackloom.worker.handlers import service_status

API = "/api/v1"


def _plant_deployment(conn, demo, *, name: str = "vit-dev-web-18"):
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
        "VALUES (%s, 'create', %s, %s, 'f18', 'succeeded', %s)",
        (change_id, project["id"], env["id"], alice["id"]),
    )
    conn.execute(
        "INSERT INTO stacks (id, slug, project_id, environment_id, backend_kind, backend_ref) "
        "VALUES (%s, %s, %s, %s, 'local_demo', 'f18')",
        (stack_id, f"f18-{stack_id.hex}", project["id"], env["id"]),
    )
    conn.execute(
        "INSERT INTO deployments (id, project_id, environment_id, template_slug, template_version_id, "
        "stack_id, created_by_change_id, owner_id, status, service_status, instances) "
        "VALUES (%s, %s, %s, 'ubuntu-web', %s, %s, %s, %s, 'active', 'unknown', %s)",
        (
            deployment_id,
            project["id"],
            env["id"],
            tpl["id"],
            stack_id,
            change_id,
            alice["id"],
            Jsonb([{"name": name, "size_tier": "standard", "vcpu": 2, "memory_mb": 4096, "disk_gb": 40}]),
        ),
    )
    conn.execute("UPDATE changes SET deployment_id = %s WHERE id = %s", (deployment_id, change_id))
    conn.execute(
        "INSERT INTO hosts (id, project_id, environment_id, deployment_id, template_version_id, change_id, "
        "stack_id, vm_stable_id, name, deployment_state) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'verified')",
        (
            host_id,
            project["id"],
            env["id"],
            deployment_id,
            tpl["id"],
            change_id,
            stack_id,
            "vm-f18-1",
            name,
        ),
    )
    conn.commit()
    return conn.execute("SELECT * FROM deployments WHERE id = %s", (deployment_id,)).fetchone()


def test_f18_segments_builtin(conn):
    segs = probes.ensure_segments(conn)
    conn.commit()
    slugs = {s["slug"] for s in segs}
    assert {"lab-access", "filtered"} <= slugs
    filtered = next(s for s in segs if s["slug"] == "filtered")
    assert filtered["reaches_targets"] is False


def test_f18_service_status_unavailable_when_probe_fails_service_active():
    """Recette : systemd/processus actif + sonde KO → unavailable."""
    host_results = [
        {
            "host": "web-01",
            "state": "checked",
            "checks": [
                {"type": "package_installed", "ok": True, "evidence": "dpkg : 1.0"},
                {"type": "service_active", "ok": True, "evidence": "processus nginx actif"},
                {
                    "type": "http_probe",
                    "ok": False,
                    "failure": "sonde HTTP en échec",
                    "evidence": "sonde : segment filtrant",
                },
            ],
        }
    ]
    status, reason = service_status(host_results)
    assert status == "unavailable"
    assert "actif" in reason
    assert "sonde" in reason


def test_f18_dimensions_distinctes():
    now = datetime.now(UTC)
    dims = probes.evaluate_dimensions(
        service_state="active",
        probe_health="failed",
        checked_at=now,
        max_age_seconds=900,
        now=now,
    )
    assert dims["service_state"] == "active"
    assert dims["probe_health"] == "failed"
    assert dims["freshness"] == "fresh"
    assert dims["availability"] == "unavailable"

    stale = probes.evaluate_dimensions(
        service_state="active",
        probe_health="ok",
        checked_at=now - timedelta(hours=2),
        max_age_seconds=900,
        now=now,
    )
    assert stale["freshness"] == "stale"
    assert stale["availability"] == "available"  # fraîcheur distincte de la disponibilité


def test_f18_filtered_segment_marks_unavailable(conn, demo, api):
    """Nœud atteignable côté OS, segment filtrant → indisponible."""
    probes.ensure_segments(conn)
    dep = _plant_deployment(conn, demo)
    probes.create_objective(
        conn,
        project_id=dep["project_id"],
        deployment_id=dep["id"],
        name="Dispo labo filtrée",
        probe_segment_slug="filtered",
        target_pct=99.0,
        created_by=demo.user("alice")["id"],
    )
    conn.commit()

    result = (
        api("alice")
        .post(
            f"{API}/deployments/{dep['id']}/probes/run",
            json={"segment": "filtered", "service_state": "active", "force_probe_ok": True},
        )
        .json()
    )
    assert result["service_status"] == "unavailable"
    assert result["segment"]["slug"] == "filtered"
    assert result["segment"]["reaches_targets"] is False
    assert result["hosts"][0]["service_state"] == "active"
    assert result["hosts"][0]["probe_health"] == "failed"
    assert result["hosts"][0]["availability"] == "unavailable"

    report = api("alice").get(f"{API}/deployments/{dep['id']}/availability").json()
    assert report["service_status"] == "unavailable"
    assert report["dimensions"]["service_state"] == "active"
    assert report["dimensions"]["probe_health"] == "failed"
    assert report["dimensions"]["freshness"] == "fresh"
    assert report["dimensions"]["availability"] == "unavailable"
    assert report["objective"]["probe_segment_slug"] == "filtered"
    assert "distincts" in report["notice"]

    card = api("alice").get(f"{API}/deployments/{dep['id']}/service-card").json()
    assert card["status"] == "unavailable"
    assert card["availability"]["service_state"] == "active"
    assert card["availability"]["probe_health"] == "failed"
    assert card["availability"]["from_segment"] == "filtered"


def test_f18_lab_access_ready_when_probe_ok(conn, demo, api):
    probes.ensure_segments(conn)
    dep = _plant_deployment(conn, demo, name="vit-dev-web-18b")
    conn.commit()
    result = (
        api("alice")
        .post(
            f"{API}/deployments/{dep['id']}/probes/run",
            json={"segment": "lab-access", "service_state": "active", "force_probe_ok": True},
        )
        .json()
    )
    assert result["service_status"] == "ready"
    assert result["hosts"][0]["availability"] == "available"


def test_f18_protocol_refused_on_segment(conn, demo, api):
    probes.ensure_segments(conn)
    # Segment custom with only tcp — plant via SQL for the refusal path.
    conn.execute(
        "INSERT INTO probe_segments (id, slug, name, zone_slug, network_name, allowed_protocols, "
        "reaches_targets, is_builtin, simulated) "
        "VALUES (%s, 'tcp-only', 'TCP seul', 'dev', 'rn-lab', ARRAY['tcp']::text[], true, false, true)",
        (uuid7(),),
    )
    dep = _plant_deployment(conn, demo, name="vit-dev-web-18c")
    conn.commit()
    resp = api("alice").post(
        f"{API}/deployments/{dep['id']}/probes/run",
        json={"segment": "tcp-only", "service_state": "active"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "protocol_not_allowed_on_segment"


def test_f18_hors_perimetre_404(conn, demo, api):
    probes.ensure_segments(conn)
    dep = _plant_deployment(conn, demo, name="vit-dev-web-18d")
    conn.commit()
    # Chloé (Compta) n'a pas accès au projet vitrine.
    resp = api("chloe").get(f"{API}/deployments/{dep['id']}/availability")
    assert resp.status_code == 404


def test_f18_admin_probe_segments(api, conn):
    probes.ensure_segments(conn)
    conn.commit()
    rows = api("admin").get(f"{API}/admin/probe-segments").json()
    assert {r["slug"] for r in rows} >= {"lab-access", "filtered"}
    status = api("admin").get(f"{API}/platform/status").json()
    assert "probe_segments" in status
    assert any(s["slug"] == "filtered" for s in status["probe_segments"])
