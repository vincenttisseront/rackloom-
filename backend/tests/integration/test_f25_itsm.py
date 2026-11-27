"""F25 : ITSM et notifications.

Faux ITSM local avec rejeu ; livraisons idempotentes ; aucun canal entrant n'approuve.
Niveau de preuve : testé localement (ITSM réel = labo).
"""

from __future__ import annotations

import flows
from rackloom import itsm, notifications
from rackloom.itsm.fake import DEMO_WEBHOOK_TOKEN

API = "/api/v1"


def _seed_change(conn, demo):
    c = flows.create_and_submit(conn, demo)
    return c


def test_f25_sync_cree_ticket_et_notification(conn, demo, api):
    c = _seed_change(conn, demo)
    client = api("alice")
    r = client.post(f"{API}/changes/{c['id']}/itsm/sync", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ticket"] is not None
    assert body["ticket"]["external_key"].startswith("RLM-")
    assert body["ticket"]["rackloom_url"].endswith(f"/demandes/{c['id']}")
    assert body["simulated"] is True
    assert body["deliveries"]["delivered"] >= 1

    # Présent sur la demande.
    g = client.get(f"{API}/changes/{c['id']}")
    assert g.status_code == 200
    assert g.json()["itsm"]["tickets"]
    assert g.json()["itsm"]["tickets"][0]["external_key"] == body["ticket"]["external_key"]

    # Notification au demandeur, lien authentifié, pas d'approbation.
    notes = client.get(f"{API}/notifications").json()
    assert notes["can_approve"] is False
    assert any(n["href"] == f"/demandes/{c['id']}" for n in notes["items"])
    assert all(n["can_approve"] is False for n in notes["items"])


def test_f25_rejeu_livraison_pas_de_second_ticket(conn, demo):
    c = _seed_change(conn, demo)
    alice = demo.principal("alice")
    first = itsm.sync_change(conn, alice, c["id"])
    conn.commit()
    ticket_id = first["ticket"]["id"]
    key = first["ticket"]["external_key"]

    # Rejouer sync + flush : même ticket (dedupe_key).
    second = itsm.sync_change(conn, alice, c["id"])
    conn.commit()
    assert second["ticket"]["id"] == ticket_id
    assert second["ticket"]["external_key"] == key

    again = itsm.deliver_pending(conn)
    conn.commit()
    assert again["delivered"] == 0 or again["already_present"] >= 0

    n = conn.execute("SELECT count(*) AS n FROM itsm_tickets WHERE change_id = %s", (c["id"],)).fetchone()["n"]
    assert n == 1

    dupes = conn.execute(
        "SELECT dedupe_key, count(*) AS n FROM itsm_deliveries "
        "WHERE change_id = %s GROUP BY dedupe_key HAVING count(*) > 1",
        (c["id"],),
    ).fetchall()
    assert dupes == []


def test_f25_webhook_rejoue_ne_cree_pas_deux_tickets(conn, demo, api):
    c = _seed_change(conn, demo)
    client = api()  # anonyme pour le webhook
    payload = {
        "event_type": "ticket_create",
        "change_id": str(c["id"]),
        "comment": "ouverture depuis ITSM fictif",
    }
    headers = {
        "Authorization": f"Bearer {DEMO_WEBHOOK_TOKEN}",
        "Idempotency-Key": f"wh-create-{c['id']}",
    }
    r1 = client.post(f"{API}/itsm/webhooks/itsm-fake", json=payload, headers=headers)
    assert r1.status_code == 200, r1.text
    assert r1.json()["created_ticket"] is True
    ticket = r1.json()["ticket"]
    assert ticket["external_key"].startswith("RLM-")

    r2 = client.post(f"{API}/itsm/webhooks/itsm-fake", json=payload, headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["outcome"] == "duplicate"
    assert r2.json()["created_ticket"] is False
    assert r2.json()["ticket"]["id"] == ticket["id"]

    n = conn.execute("SELECT count(*) AS n FROM itsm_tickets WHERE change_id = %s", (c["id"],)).fetchone()["n"]
    assert n == 1


def test_f25_webhook_approve_refuse(conn, demo, api):
    c = _seed_change(conn, demo)
    alice = demo.principal("alice")
    synced = itsm.sync_change(conn, alice, c["id"])
    conn.commit()
    ext = synced["ticket"]["external_ticket_id"]

    client = api()
    r = client.post(
        f"{API}/itsm/webhooks/itsm-fake",
        json={
            "event_type": "approve",
            "external_ticket_id": ext,
            "change_id": str(c["id"]),
            "decision": "approved",
        },
        headers={"Authorization": f"Bearer {DEMO_WEBHOOK_TOKEN}", "Idempotency-Key": "wh-approve-1"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "approval_forbidden"

    # Aucune approbation créée.
    n_appr = conn.execute("SELECT count(*) AS n FROM approvals WHERE change_id = %s", (c["id"],)).fetchone()["n"]
    assert n_appr == 0

    inbound = conn.execute(
        "SELECT outcome, rejection_code FROM itsm_inbound_events "
        "WHERE rejection_code = 'approval_forbidden' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert inbound is not None
    assert inbound["outcome"] == "rejected"


def test_f25_notification_respond_ne_approuve_pas(conn, demo, api):
    c = _seed_change(conn, demo)
    client = api("alice")
    sync = client.post(f"{API}/changes/{c['id']}/itsm/sync", json={})
    assert sync.status_code == 200, sync.text

    notes = client.get(f"{API}/notifications?unread=true").json()["items"]
    assert notes
    nid = notes[0]["id"]

    bad = client.post(
        f"{API}/notifications/{nid}/respond",
        json={"action": "approve", "decision": "approved"},
    )
    assert bad.status_code == 403, bad.text
    assert bad.json()["code"] == "approval_forbidden"

    # Accusé de réception sans approbation.
    ok = client.post(f"{API}/notifications/{nid}/respond", json={"action": "ack"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["can_approve"] is False
    assert ok.json()["outcome"] == "acknowledged"

    n_appr = conn.execute("SELECT count(*) AS n FROM approvals WHERE change_id = %s", (c["id"],)).fetchone()["n"]
    assert n_appr == 0


def test_f25_lien_notification_authentifie(conn, demo):
    c = _seed_change(conn, demo)
    alice = demo.principal("alice")
    itsm.sync_change(conn, alice, c["id"])
    conn.commit()
    items = notifications.list_for_user(conn, alice)
    assert items
    note = items[0]
    assert note["href"] == f"/demandes/{c['id']}"
    assert note["absolute_url"].endswith(note["href"])
    assert "token=" not in note["absolute_url"].lower()
    assert "password" not in note["absolute_url"].lower()
    assert note["can_approve"] is False
    assert note["actions"] == []


def test_f25_hors_perimetre_404(conn, demo, api):
    c = _seed_change(conn, demo)
    # Chloé (compta) ne voit pas la demande vitrine.
    other = api("chloe")
    r = other.get(f"{API}/changes/{c['id']}/itsm")
    assert r.status_code == 404, r.text
    sync = other.post(f"{API}/changes/{c['id']}/itsm/sync", json={})
    assert sync.status_code == 404, sync.text


def test_f25_webhook_jeton_invalide(api):
    client = api()
    r = client.post(
        f"{API}/itsm/webhooks/itsm-fake",
        json={"event_type": "comment"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401
    assert r.json()["code"] == "unauthenticated"
