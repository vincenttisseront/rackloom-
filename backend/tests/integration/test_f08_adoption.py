"""F08 : adoption de VM existantes.

Enrôlement + observation fictive lecture seule ; aperçu sans mutation ; proposition
séparée de l'exécution ; conflit de propriété → import bloqué.
Niveau de preuve : testé localement (observations fictives explicites, D2).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from rackloom import adoption
from rackloom.errors import ApiError

API = "/api/v1"


def test_f08_preview_ne_modifie_pas_la_vm(conn, demo, api):
    alice = demo.principal("alice")
    # Empreinte inventaire avant.
    before = conn.execute(
        "SELECT attributes FROM inventory_items WHERE kind = 'vm' AND stable_id = 'vm-9001' "
        "ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()
    assert before is not None
    claims_before = conn.execute(
        "SELECT count(*) AS n FROM terraform_resource_claims WHERE vm_stable_id = 'vm-9001'"
    ).fetchone()["n"]

    enrolled = adoption.enroll(
        conn, alice, "vitrine", environment_slug="dev", vm_stable_id="vm-9001"
    )
    conn.commit()
    assert enrolled["state"] == "observing"
    assert enrolled["observation_fictional"] is True
    assert enrolled["observation"]["fictional"] is True
    assert enrolled["observation"]["mutation"] is False

    observed = adoption.observe(conn, alice, UUID(enrolled["id"]))
    conn.commit()
    assert observed["observation_fictional"] is True

    previewed = adoption.preview_import(conn, alice, UUID(enrolled["id"]))
    conn.commit()
    assert previewed["preview_result"]["preview"] is True
    assert previewed["preview_result"]["mutation"] is False
    assert previewed["preview_result"]["import_executed"] is False
    assert previewed["state"] == "preview_ready"

    after = conn.execute(
        "SELECT attributes FROM inventory_items WHERE kind = 'vm' AND stable_id = 'vm-9001' "
        "ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()
    assert after["attributes"] == before["attributes"]
    claims_after = conn.execute(
        "SELECT count(*) AS n FROM terraform_resource_claims WHERE vm_stable_id = 'vm-9001'"
    ).fetchone()["n"]
    assert claims_after == claims_before

    # Via API également.
    client = api("alice")
    r = client.get(f"{API}/projects/vitrine/adoptions/candidates?environment=dev")
    assert r.status_code == 200
    body = r.json()
    assert body["fictional"] is True
    ids = {c["vm_stable_id"] for c in body["candidates"]}
    assert "vm-9001" in ids
    assert "vm-9002" in ids


def test_f08_conflit_propriete_bloque_import(conn, demo, api, worker):
    alice = demo.principal("alice")
    bruno = demo.principal("bruno")

    enrolled = adoption.enroll(
        conn, alice, "vitrine", environment_slug="dev", vm_stable_id="vm-9002"
    )
    conn.commit()
    assert enrolled["state"] == "blocked_conflict"
    assert enrolled["ownership"] == "conflict"

    # L'aperçu reste possible et ne mute rien.
    previewed = adoption.preview_import(conn, alice, UUID(enrolled["id"]))
    conn.commit()
    assert previewed["preview_result"]["mutation"] is False
    assert previewed["preview_result"]["ownership"]["conflict"] is True

    with pytest.raises(ApiError) as exc:
        adoption.propose_import(conn, alice, UUID(enrolled["id"]))
    assert exc.value.code == "ownership_conflict"
    conn.commit()  # commit=True sur l'erreur

    row = conn.execute(
        "SELECT state FROM vm_adoptions WHERE id = %s", (enrolled["id"],)
    ).fetchone()
    assert row["state"] == "blocked_conflict"

    # Import via API aussi refusé.
    client = api("alice")
    # Ré-enrôlement impossible (déjà présent) — propose sur l'id existant.
    r = client.post(f"{API}/adoptions/{enrolled['id']}/propose-import")
    assert r.status_code == 409
    assert r.json()["code"] == "ownership_conflict"

    # Parcours nominal sur vm-9001 pour contraste : propose → approve → import.
    ok = adoption.enroll(
        conn, alice, "vitrine", environment_slug="dev", vm_stable_id="vm-9001"
    )
    conn.commit()
    adoption.preview_import(conn, alice, UUID(ok["id"]))
    proposed = adoption.propose_import(conn, alice, UUID(ok["id"]))
    conn.commit()
    assert proposed["state"] == "import_proposed"
    assert proposed["import_proposal_sha256"]
    assert proposed["import_proposal"]["simulated"] is True

    approved = adoption.approve_import(conn, bruno, UUID(ok["id"]))
    conn.commit()
    assert approved["approved_at"]
    assert approved["approval_proposal_sha256"] == proposed["import_proposal_sha256"]

    imported = adoption.execute_import(conn, alice, UUID(ok["id"]))
    conn.commit()
    assert imported["state"] == "imported"
    assert imported["ownership"] == "rackloom"
    claim = conn.execute(
        "SELECT * FROM terraform_resource_claims WHERE vm_stable_id = 'vm-9001'"
    ).fetchone()
    assert claim is not None
    assert claim["claim_source"] == "rackloom"
    assert claim["simulated"] is True

    # Worker drain du job tf_import simulé.
    assert worker.drain(kinds=["tf_import"]) == ["tf_import"]
    job = conn.execute(
        "SELECT state, result FROM jobs WHERE id = %s", (imported["import_job_id"],)
    ).fetchone()
    assert job["state"] == "succeeded"
    assert job["result"]["simulated"] is True


def test_f08_hors_perimetre_404(api):
    # Chloé (compta) ne voit pas les adoptions vitrine.
    client = api("chloe")
    r = client.get(f"{API}/projects/vitrine/adoptions")
    assert r.status_code == 404
