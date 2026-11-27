"""F03 : réservations IPAM/DNS (adaptateur fictif).

Prévisualisation sans réservation ; concurrence → adresses distinctes ; état incertain
→ IP conservée (held_uncertain) ; libération refusée tant que non réconcilié.
Niveau de preuve : testé localement (pas d'IPAM réel).
"""

from __future__ import annotations

import threading

import pytest

import flows
from rackloom import ipam
from rackloom.errors import ApiError
from rackloom.policy import BUILTIN_VERSION


def test_f03_preview_ne_reserve_pas(conn, demo):
    before = conn.execute("SELECT count(*) AS n FROM ipam_reservations").fetchone()["n"]
    preview = ipam.preview(conn, hostname="vit-dev-web-99")
    assert preview["reservation"] is False
    assert preview["preview"] is True
    assert preview["address"].startswith("192.0.2.")
    assert preview["dns_name"].endswith("lab.demo.invalid")
    conn.commit()
    after = conn.execute("SELECT count(*) AS n FROM ipam_reservations").fetchone()["n"]
    assert after == before


def test_f03_concurrence_adresses_distinctes(conn, demo, db_url):
    import psycopg
    from psycopg.rows import dict_row

    pool = ipam.default_pool(conn)
    conn.commit()
    results: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def worker(host: str) -> None:
        c = psycopg.connect(db_url, row_factory=dict_row)
        try:
            barrier.wait(timeout=5)
            for attempt in range(5):
                try:
                    row = ipam.prepare(c, hostname=host)
                    c.commit()
                    with lock:
                        results.append(row["address"])
                    return
                except psycopg.errors.DeadlockDetected:
                    c.rollback()
                    if attempt == 4:
                        raise
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
            c.rollback()
        finally:
            c.close()

    t1 = threading.Thread(target=worker, args=("host-a",))
    t2 = threading.Thread(target=worker, args=("host-b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not errors, errors
    assert len(results) == 2
    assert results[0] != results[1]
    assert pool["slug"] == "lab-demo"
    # Les deux réservations existent bien.
    n = conn.execute(
        "SELECT count(*) AS n FROM ipam_reservations WHERE state = 'prepared'"
    ).fetchone()["n"]
    assert n >= 2


def test_f03_incertain_ne_libere_pas(conn, demo):
    from uuid import UUID

    prepared = ipam.prepare(conn, hostname="hold-me")
    reserved = ipam.reserve(conn, UUID(prepared["id"]))
    held = ipam.hold_uncertain(conn, UUID(reserved["id"]), reason="tf_apply résultat inconnu")
    conn.commit()
    assert held["state"] == "held_uncertain"
    with pytest.raises(ApiError) as exc:
        ipam.release(conn, UUID(held["id"]))
    assert exc.value.code == "ipam_held_uncertain"
    conn.rollback()
    row = conn.execute(
        "SELECT state, host(address)::text AS address FROM ipam_reservations WHERE id = %s",
        (held["id"],),
    ).fetchone()
    assert row["state"] == "held_uncertain"
    assert row["address"] == held["address"]


@pytest.mark.terraform
def test_f03_admission_reserve_puis_confirme(conn, demo, worker):
    c = flows.create_and_submit(conn, demo)
    assert flows.drain_plan(worker) == ["preflight", "tf_plan"]
    plan = flows.latest_plan(conn, c["id"])
    # Prévisualisation dans la résolution : aucune ligne de réservation encore.
    resolved = conn.execute(
        "SELECT resolved FROM request_revisions WHERE change_id = %s AND revision_no = %s",
        (c["id"], c["revision_no"]),
    ).fetchone()["resolved"]
    preview = resolved["placement"]["ipam_preview"]
    assert preview["reservation"] is False
    assert preview["candidates"]

    res = flows.approve(conn, demo, c["id"], plan)
    assert worker.drain(kinds=["tf_apply"]) == ["tf_apply"]
    job = flows.job(conn, res["job_id"])
    assert job["state"] == "succeeded", job

    rows = ipam.for_change(conn, c["id"])
    # Après succès, confirmées (for_change ne liste que ACTIVE : confirmed inclus).
    assert rows and all(r["state"] == "confirmed" for r in rows)
    assert BUILTIN_VERSION == "builtin@10"

    decisions = conn.execute(
        "SELECT rules FROM admission_decisions WHERE job_id = %s AND stage = 'start'",
        (res["job_id"],),
    ).fetchone()
    assert any(r["id"] == "R120" and r["ok"] for r in decisions["rules"])
