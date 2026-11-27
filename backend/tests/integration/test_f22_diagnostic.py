"""F22 : dossier de diagnostic partageable.

Export filtré (période, symptômes, preuves, événements, manquants) ; canari absent
de l'export et du résumé ; téléchargement interprojet → 404.
Niveau de preuve : testé localement.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rackloom import artifacts, events, security
from rackloom.config import get_settings
from rackloom.redact import REDACTED

API = "/api/v1"
CANARY = "RLCANARY-f22deadbeefdeadbeef"


@pytest.fixture
def f22_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    keys = tmp_path / "keys"
    data = tmp_path / "data"
    security.generate_keys(keys)
    monkeypatch.setenv("RACKLOOM_KEYS_DIR", str(keys))
    monkeypatch.setenv("RACKLOOM_DATA_DIR", str(data))
    get_settings.cache_clear()
    return data


def test_f22_export_filtre_canari_et_resume(api, conn, demo, f22_keys):
    import flows

    _ = f22_keys
    c = flows.create_and_submit(conn, demo)
    # Canari dans un événement (journal) et dans un artefact de classe A (ne doit jamais fuir).
    events.emit(
        conn,
        type="runner.log",
        source="ansible",
        project_id=demo.project("vitrine")["id"],
        correlation_id=c["id"],
        job_id=c["job_id"],
        data={"message": f"password={CANARY} ligne de journal"},
        severity="warning",
    )
    artifacts.store(
        conn,
        project_id=demo.project("vitrine")["id"],
        change_id=c["id"],
        kind="tf_plan_bin",
        key_class="A",
        plaintext=f"SECRET-PLAN-{CANARY}".encode(),
    )
    # Rapport B légitime (métadonnée seulement dans le dossier).
    artifacts.store(
        conn,
        project_id=demo.project("vitrine")["id"],
        change_id=c["id"],
        kind="ansible_configure_report",
        key_class="B",
        plaintext=b'{"run":"configure","exit_code":0,"hosts":{}}',
    )
    conn.commit()

    alice = api("alice")
    created = alice.post(
        f"{API}/changes/{c['id']}/diagnostic-packs",
        json={"symptoms": [f"fuite password={CANARY}"], "include_summary": True},
    )
    assert created.status_code == 200, created.text
    pack = created.json()
    assert pack["schema"] == "rackloom.diagnostic_pack/v1"
    assert pack["event_count"] >= 1
    assert pack["evidence_count"] >= 1
    assert pack["summary"] is not None
    assert pack["period"]["start"]
    assert pack["missing"]  # au moins no_hosts / no_observations en début de parcours
    meta_dump = json.dumps(pack, ensure_ascii=False)
    assert CANARY not in meta_dump
    assert REDACTED in json.dumps(pack["symptoms"], ensure_ascii=False) or all(
        CANARY not in json.dumps(s, ensure_ascii=False) for s in pack["symptoms"]
    )

    # Téléchargement : export complet sans canari.
    dl = alice.get(f"{API}/diagnostic-packs/{pack['id']}/download")
    assert dl.status_code == 200, dl.text
    assert "application/json" in dl.headers["content-type"]
    body = dl.content.decode("utf-8")
    assert CANARY not in body
    report = json.loads(body)
    assert report["schema"] == "rackloom.diagnostic_pack/v1"
    assert "events" in report and "symptoms" in report and "evidence" in report and "missing" in report
    assert report["summary"] is not None
    assert CANARY not in json.dumps(report["summary"], ensure_ascii=False)
    # Preuves : uniquement classe B ; pas de contenu de plan A.
    assert all(e["key_class"] == "B" for e in report["evidence"])
    assert "SECRET-PLAN" not in body
    assert "tf_plan_bin" not in {e["kind"] for e in report["evidence"]}

    # Liste + détail
    listed = alice.get(f"{API}/changes/{c['id']}/diagnostic-packs")
    assert listed.status_code == 200
    assert any(p["id"] == pack["id"] for p in listed.json())
    detail = alice.get(f"{API}/diagnostic-packs/{pack['id']}")
    assert detail.status_code == 200
    assert detail.json()["digest"] == pack["digest"]


def test_f22_telechargement_interprojet_404(api, conn, demo, f22_keys):
    import flows

    _ = f22_keys
    c = flows.create_and_submit(conn, demo)
    conn.commit()
    alice = api("alice")
    pack = alice.post(f"{API}/changes/{c['id']}/diagnostic-packs", json={}).json()

    chloe = api("chloe")
    assert chloe.get(f"{API}/changes/{c['id']}/diagnostic-packs").status_code == 404
    assert chloe.post(f"{API}/changes/{c['id']}/diagnostic-packs", json={}).status_code == 404
    assert chloe.get(f"{API}/diagnostic-packs/{pack['id']}").status_code == 404
    assert chloe.get(f"{API}/diagnostic-packs/{pack['id']}/download").status_code == 404
    # Artefact sous-jacent aussi hors périmètre.
    assert chloe.get(f"{API}/artifacts/{pack['artifact_id']}").status_code == 404


def test_f22_periode_invalide(api, conn, demo, f22_keys):
    import flows

    _ = f22_keys
    c = flows.create_and_submit(conn, demo)
    conn.commit()
    alice = api("alice")
    bad = alice.post(
        f"{API}/changes/{c['id']}/diagnostic-packs",
        json={"period_start": "2026-11-27T12:00:00Z", "period_end": "2026-11-27T11:00:00Z"},
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "invalid_period"
