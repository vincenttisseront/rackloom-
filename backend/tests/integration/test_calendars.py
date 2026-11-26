"""F27 : calendriers de changement — fenêtres, gels planifiés, horloge injectée, DST Europe/Paris.

Niveau de preuve : testé localement.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import flows
from rackloom import clock
from rackloom.db import transaction
from rackloom.projects import calendars

pytestmark = pytest.mark.terraform
PARIS = ZoneInfo("Europe/Paris")


def _env(demo):
    return demo.env("vitrine", "dev")


def _principal(demo, name="bruno"):
    return demo.principal(name)


def test_f27_aucune_fenetre_ecritures_ouvertes(conn, demo):
    env = _env(demo)
    assert calendars.writable(conn, env["id"])["ok"] is True


def test_f27_hors_fenetre_refuse_avec_horloge(conn, demo):
    env = _env(demo)
    bruno = _principal(demo)
    # Fenêtre lundi 10:00–12:00 Europe/Paris.
    calendars.set_timezone(conn, bruno, env["id"], "Europe/Paris")
    calendars.add_window(conn, bruno, env["id"], {
        "weekdays": [0], "start_local": "10:00", "end_local": "12:00", "label": "lundi matin",
    })
    conn.commit()
    # Mardi 11:00 Paris → hors fenêtre.
    tuesday = datetime(2026, 3, 24, 11, 0, tzinfo=PARIS).astimezone(UTC)
    with clock.frozen_at(tuesday):
        state = calendars.writable(conn, env["id"])
    assert state["ok"] is False and state["code"] == "outside_change_window"
    assert state["next_open"] is not None
    # Lundi 11:00 Paris → dans la fenêtre.
    monday = datetime(2026, 3, 23, 11, 0, tzinfo=PARIS).astimezone(UTC)
    with clock.frozen_at(monday):
        assert calendars.writable(conn, env["id"])["ok"] is True


def test_f27_dst_europe_paris_changement_d_heure(conn, demo):
    """Fenêtre traversant le passage à l'heure d'été 2026-03-29 (02:00 → 03:00)."""
    env = _env(demo)
    bruno = _principal(demo)
    calendars.set_timezone(conn, bruno, env["id"], "Europe/Paris")
    calendars.add_window(conn, bruno, env["id"], {
        "weekdays": [6], "start_local": "01:30", "end_local": "03:30", "label": "DST",
    })
    conn.commit()
    # Dimanche 29 mars 2026 : 02:00 locale n'existe pas ; 01:30 CET puis 03:00 CEST.
    before = datetime(2026, 3, 29, 1, 45, tzinfo=PARIS).astimezone(UTC)
    with clock.frozen_at(before):
        assert calendars.writable(conn, env["id"])["ok"] is True
    after = datetime(2026, 3, 29, 3, 15, tzinfo=PARIS).astimezone(UTC)
    with clock.frozen_at(after):
        assert calendars.writable(conn, env["id"])["ok"] is True
    # Juste après la fin locale 03:30 CEST.
    closed = datetime(2026, 3, 29, 3, 45, tzinfo=PARIS).astimezone(UTC)
    with clock.frozen_at(closed):
        state = calendars.writable(conn, env["id"])
    assert state["ok"] is False and state["code"] == "outside_change_window"
    # Prochaine ouverture : dimanche suivant 01:30 (DST déjà appliqué).
    nxt = calendars.next_open(conn, env["id"], after=closed)
    assert nxt is not None
    local = nxt.astimezone(PARIS)
    assert local.weekday() == 6 and (local.hour, local.minute) == (1, 30)


def test_f27_periode_de_gel_calendaire_refusee(conn, demo, worker):
    env = _env(demo)
    bruno = _principal(demo)
    now = clock.now()
    calendars.add_freeze(conn, bruno, env["id"], {
        "starts_at": (now - timedelta(minutes=5)).isoformat(),
        "ends_at": (now + timedelta(hours=2)).isoformat(),
        "reason": "Fenêtre de gel planifiée pour maintenance réseau",
    })
    conn.commit()
    state = calendars.writable(conn, env["id"])
    assert state["ok"] is False and state["code"] == "calendar_frozen"

    c, plan, job_id = _approve_now(conn, demo, worker)
    job = flows.job(conn, job_id)
    assert job["state"] == "queued"
    # Programmé après le gel : forcer la réclamation pour exercer R090.
    with transaction() as tx:
        tx.execute("UPDATE jobs SET run_after = now() - interval '1 second' WHERE id = %s", (job_id,))
    worker.drain(kinds=["tf_apply"])
    job = flows.job(conn, job_id)
    assert job["state"] == "blocked" and job["blocked_reason"] == "calendar_frozen"
    assert flows.change(conn, c["id"])["state"] == "blocked"


def test_f27_plan_expire_pendant_attente_fenetre(conn, demo, worker):
    """Plan programmé pour une fenêtre future : à l'ouverture, R060 refuse (plan_expired)."""
    env = _env(demo)
    bruno = _principal(demo)
    calendars.set_timezone(conn, bruno, env["id"], "Europe/Paris")
    # Seulement le lundi 10–12.
    calendars.add_window(conn, bruno, env["id"], {
        "weekdays": [0], "start_local": "10:00", "end_local": "12:00", "label": "lundi",
    })
    conn.commit()
    # Mardi 9:00 : hors fenêtre → run_after = lundi suivant 10:00.
    tuesday = datetime(2026, 3, 24, 9, 0, tzinfo=PARIS).astimezone(UTC)
    with clock.frozen_at(tuesday):
        c = flows.create_and_submit(conn, demo)
        flows.drain_plan(worker)
        plan = flows.latest_plan(conn, c["id"])
        # TTL court : le plan expire avant lundi.
        conn.execute("UPDATE plans SET valid_until = %s WHERE id = %s",
                     (tuesday + timedelta(hours=1), plan["id"]))
        conn.commit()
        res = flows.approve(conn, demo, c["id"], plan)
        job = flows.job(conn, res["job_id"])
        assert job["run_after"] > tuesday
        assert job["state"] == "queued"

    monday_open = datetime(2026, 3, 30, 10, 5, tzinfo=PARIS).astimezone(UTC)
    # Avancer run_after pour que le claim SQL (now() réel) puisse prendre le job, horloge Python au lundi.
    with transaction() as tx:
        tx.execute("UPDATE jobs SET run_after = now() - interval '1 second' WHERE id = %s", (res["job_id"],))
    with clock.frozen_at(monday_open):
        worker.drain(kinds=["tf_apply"])
    job = flows.job(conn, res["job_id"])
    assert job["state"] == "blocked" and job["blocked_reason"] == "plan_expired"
    assert flows.change(conn, c["id"])["state"] == "plan_expired"
    assert flows.latest_plan(conn, c["id"])["status"] == "expired"


def test_f27_job_hors_fenetre_refuse_a_admission(conn, demo, worker):
    env = _env(demo)
    bruno = _principal(demo)
    calendars.set_timezone(conn, bruno, env["id"], "Europe/Paris")
    calendars.add_window(conn, bruno, env["id"], {
        "weekdays": [0], "start_local": "10:00", "end_local": "12:00", "label": "lundi",
    })
    conn.commit()
    monday = datetime(2026, 3, 23, 11, 0, tzinfo=PARIS).astimezone(UTC)
    with clock.frozen_at(monday):
        c, plan, job_id = _approve_now(conn, demo, worker)
    # Claim forcé hors fenêtre (mercredi).
    wednesday = datetime(2026, 3, 25, 11, 0, tzinfo=PARIS).astimezone(UTC)
    with transaction() as tx:
        tx.execute("UPDATE jobs SET run_after = now() - interval '1 second' WHERE id = %s", (job_id,))
    with clock.frozen_at(wednesday):
        worker.drain(kinds=["tf_apply"])
    job = flows.job(conn, job_id)
    assert job["state"] == "blocked" and job["blocked_reason"] == "outside_change_window"
    decisions = conn.execute(
        "SELECT rules FROM admission_decisions WHERE job_id = %s ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()["rules"]
    assert any(r["id"] == "R090" and not r["ok"] for r in decisions)


def test_f27_r090_aussi_a_dispatch(conn, demo, worker, monkeypatch):
    from rackloom.worker import handlers

    env = _env(demo)
    bruno = _principal(demo)
    now = clock.now()
    c, plan, job_id = _approve_now(conn, demo, worker)
    original = handlers._prepare_tf

    def prepare_then_freeze(*args, **kwargs):
        work = original(*args, **kwargs)
        calendars.add_freeze(conn, bruno, env["id"], {
            "starts_at": (now - timedelta(minutes=1)).isoformat(),
            "ends_at": (now + timedelta(hours=1)).isoformat(),
            "reason": "Gel calendaire entre admission et envoi",
        })
        conn.commit()
        return work

    monkeypatch.setattr(handlers, "_prepare_tf", prepare_then_freeze)
    assert worker.drain(kinds=["tf_apply"]) == ["tf_apply"]
    job = flows.job(conn, job_id)
    assert job["state"] == "blocked" and job["blocked_reason"] == "calendar_frozen"
    stages = [(d["stage"], d["outcome"]) for d in conn.execute(
        "SELECT stage, outcome FROM admission_decisions WHERE job_id = %s ORDER BY id", (job_id,)).fetchall()]
    assert ("start", "admitted") in stages and ("dispatch", "refused") in stages


def _approve_now(conn, demo, worker):
    c = flows.create_and_submit(conn, demo)
    flows.drain_plan(worker)
    plan = flows.latest_plan(conn, c["id"])
    res = flows.approve(conn, demo, c["id"], plan)
    return c, plan, res["job_id"]
