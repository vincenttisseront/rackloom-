"""F21 : chronologie unifiée — corrélation, sources ADR, déduplication, pas de sorties brutes."""

from __future__ import annotations

import pytest

import flows
from rackloom import events
from rackloom.events import RAW_KEYS, SOURCES

pytestmark = [pytest.mark.terraform, pytest.mark.ansible, pytest.mark.docker]

API = "/api/v1"


def _events(conn, change_id):
    conn.commit()
    return conn.execute(
        "SELECT id, type, source, correlation_id, job_id, data, dedupe_key FROM events "
        "WHERE correlation_id = %s ORDER BY id",
        (change_id,),
    ).fetchall()


def test_f21_sources_et_correlation_parcours(conn, demo, worker, lab, api):
    lab.start(1)
    c = flows.create_and_submit(conn, demo)
    assert flows.drain_plan(worker) == ["preflight", "tf_plan"]
    flows.approve(conn, demo, c["id"], flows.latest_plan(conn, c["id"]))
    assert worker.drain() == ["tf_apply", "guest_ready", "ansible_configure", "verify"]

    rows = _events(conn, c["id"])
    assert rows
    assert {str(r["correlation_id"]) for r in rows} == {str(c["id"])}
    sources = {r["source"] for r in rows}
    assert sources <= SOURCES
    for required in ("request", "approval", "job", "terraform", "vcenter", "ansible", "verification"):
        assert required in sources, (required, sources)

    types = {r["type"] for r in rows}
    assert "change.created" in types
    assert "approval.decided" in types
    assert "job.queued" in types
    assert "job.step" in types

    alice = api("alice")
    body = alice.get(f"{API}/changes/{c['id']}/events").json()
    assert body and body[0]["correlation_id"] == str(c["id"])
    assert {e["source"] for e in body} <= SOURCES
    for e in body:
        flat = _flatten_keys(e.get("data") or {})
        assert not (flat & RAW_KEYS), (e["type"], flat & RAW_KEYS)
        assert "stdout" not in flat and "plan_json" not in flat


def test_f21_dedupe_reemission_sans_doublon(conn, demo):
    c = flows.create_and_submit(conn, demo)
    first = events.emit(
        conn,
        type="job.step",
        source="terraform",
        project_id=demo.project("vitrine")["id"],
        correlation_id=c["id"],
        job_id=c["job_id"],
        data={"kind": "tf_plan", "step": "Plan", "state": "succeeded", "stdout": "RAW SECRET PLAN"},
        dedupe_key=f"f21-dedupe:{c['id']}",
    )
    assert first is not None
    second = events.emit(
        conn,
        type="job.step",
        source="terraform",
        project_id=demo.project("vitrine")["id"],
        correlation_id=c["id"],
        job_id=c["job_id"],
        data={"kind": "tf_plan", "step": "Plan", "state": "succeeded", "stdout": "AGAIN"},
        dedupe_key=f"f21-dedupe:{c['id']}",
    )
    assert second is None
    conn.commit()
    n = conn.execute("SELECT count(*) AS n FROM events WHERE dedupe_key = %s", (f"f21-dedupe:{c['id']}",)).fetchone()[
        "n"
    ]
    assert n == 1
    row = conn.execute("SELECT data FROM events WHERE dedupe_key = %s", (f"f21-dedupe:{c['id']}",)).fetchone()
    assert "stdout" not in row["data"]


def test_f21_sse_autre_projet_404_et_reprise(api, conn, demo):
    from rackloom.changes import service

    vit = service.create_change(conn, demo.principal("alice"), demo.project("vitrine"), "F21 vitrine")
    service.add_version(conn, demo.principal("alice"), vit["id"], {"need": flows.need(), "base_version_no": 0})
    cpt = service.create_change(conn, demo.principal("chloe"), demo.project("compta"), "F21 compta")
    service.add_version(conn, demo.principal("chloe"), cpt["id"], {"need": flows.need(), "base_version_no": 0})
    conn.commit()

    chloe, alice = api("chloe"), api("alice")
    assert chloe.get(f"{API}/changes/{vit['id']}/events").status_code == 404
    assert chloe.get(f"{API}/changes/{vit['id']}/events/stream").status_code == 404

    full = _sse_events(alice.get(f"{API}/changes/{vit['id']}/events/stream").text)
    assert full and {e["correlation_id"] for e in full} == {str(vit["id"])}
    cut = full[0]["id"]
    rest = _sse_events(
        alice.get(
            f"{API}/changes/{vit['id']}/events/stream",
            headers={"Last-Event-ID": str(cut)},
        ).text
    )
    assert [e["id"] for e in rest] == [e["id"] for e in full[1:]]

    cpt_stream = _sse_events(chloe.get(f"{API}/changes/{cpt['id']}/events/stream").text)
    assert cpt_stream and {e["correlation_id"] for e in cpt_stream} == {str(cpt["id"])}
    assert str(vit["id"]) not in {e["correlation_id"] for e in cpt_stream}


def _sse_events(text: str) -> list[dict]:
    import json

    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]


def _flatten_keys(obj: dict, acc: set[str] | None = None) -> set[str]:
    acc = acc if acc is not None else set()
    for k, v in obj.items():
        acc.add(k)
        if isinstance(v, dict):
            _flatten_keys(v, acc)
    return acc
