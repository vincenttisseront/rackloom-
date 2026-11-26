"""F56 : estimation de mise à disposition — pas de modèle, pas de date sans approbation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import flows
from rackloom.db import Jsonb
from rackloom.ids import uuid7
from rackloom.projects import calendars

API = "/api/v1"


def test_pas_de_date_sans_approbation_et_historique_insuffisant(api, conn, demo):
    """Demande non approuvée : aucune date garantie ; historique insuffisant → non estimable."""
    alice = api("alice")
    change = alice.post(f"{API}/projects/vitrine/changes", json={"title": "F56 brouillon"}).json()
    res = alice.post(
        f"{API}/changes/{change['id']}/versions",
        json={"need": flows.need(environment="dev"), "base_version_no": 0},
    )
    assert res.status_code == 200, res.text
    body = alice.get(f"{API}/changes/{change['id']}/estimate").json()
    assert body["model_used"] is False
    assert body["approval"]["guaranteed_delivery_at"] is None
    assert body["approval"]["status"] in ("not_ready", "pending", "n/a")
    assert body["execution"]["status"] == "insufficient_history"
    assert body["execution"]["label"] == "délai non estimable"
    assert body["queue"]["status"] == "unavailable"


def test_demande_planifiee_montre_creneau(api, conn, demo):
    env = demo.env("vitrine", "dev")
    bruno = demo.principal("bruno")
    calendars.set_timezone(conn, bruno, env["id"], "Europe/Paris")
    calendars.add_window(
        conn,
        bruno,
        env["id"],
        {"weekdays": [6], "start_local": "03:00", "end_local": "04:00", "label": "F56 test"},
    )
    conn.commit()

    alice = api("alice")
    change = alice.post(f"{API}/projects/vitrine/changes", json={"title": "F56 créneau"}).json()
    alice.post(
        f"{API}/changes/{change['id']}/versions",
        json={"need": flows.need(environment="dev"), "base_version_no": 0},
    )
    run_after = datetime.now(UTC) + timedelta(days=1)
    conn.execute(
        "UPDATE changes SET state = 'approved', environment_id = %s WHERE id = %s",
        (env["id"], change["id"]),
    )
    conn.execute(
        "INSERT INTO jobs (id, change_id, project_id, kind, class, state, payload, "
        "idempotency_key, run_after) "
        "VALUES (%s, %s, %s, 'tf_apply', 'write', 'queued', '{}'::jsonb, %s, %s)",
        (uuid7(), change["id"], demo.project("vitrine")["id"], f"f56-{change['id']}", run_after),
    )
    conn.commit()

    body = alice.get(f"{API}/changes/{change['id']}/estimate").json()
    assert body["approval"]["status"] == "done"
    assert body["approval"]["guaranteed_delivery_at"] is None
    assert body["schedule"]["timezone"] == "Europe/Paris"
    assert body["schedule"]["run_after"] is not None
    assert body["model_used"] is False


def test_fourchette_avec_historique_suffisant(api, conn, demo):
    project = demo.project("vitrine")
    env = demo.env("vitrine", "dev")
    alice_id = demo.user("alice")["id"]
    need = flows.need(environment="dev")

    for i in range(5):
        cid = uuid7()
        conn.execute(
            "INSERT INTO changes (id, project_id, environment_id, kind, title, state, created_by) "
            "VALUES (%s, %s, %s, 'create', %s, 'succeeded', %s)",
            (cid, project["id"], env["id"], f"hist-{i}", alice_id),
        )
        conn.execute(
            "INSERT INTO request_versions "
            "(id, change_id, version_no, need, provenance, need_sha256, source, created_by) "
            "VALUES (%s, %s, 1, %s, '{}'::jsonb, %s, 'api', %s)",
            (uuid7(), cid, Jsonb(need), f"sha-{i}", alice_id),
        )
        start = datetime.now(UTC) - timedelta(hours=2, minutes=i)
        for kind, offset in (("tf_apply", 0), ("ansible_configure", 5), ("verify", 10)):
            created = start + timedelta(minutes=offset)
            finished = created + timedelta(minutes=3 + i)
            conn.execute(
                "INSERT INTO jobs (id, change_id, project_id, kind, class, state, payload, "
                "idempotency_key, created_at, finished_at) "
                "VALUES (%s, %s, %s, %s, 'write', 'succeeded', '{}'::jsonb, %s, %s, %s)",
                (uuid7(), cid, project["id"], kind, f"hist-{i}-{kind}", created, finished),
            )
    conn.commit()

    change = api("alice").post(f"{API}/projects/vitrine/changes", json={"title": "ETA hist"}).json()
    api("alice").post(
        f"{API}/changes/{change['id']}/versions",
        json={"need": need, "base_version_no": 0},
    )
    conn.execute(
        "UPDATE changes SET environment_id = %s WHERE id = %s",
        (env["id"], change["id"]),
    )
    conn.commit()

    body = api("alice").get(f"{API}/changes/{change['id']}/estimate").json()
    assert body["model_used"] is False
    assert body["execution"]["status"] == "estimated"
    assert body["execution"]["range"]["p50_seconds"] > 0
    assert body["execution"]["sample_size"] >= 5
