"""F26 : exercices de restauration et interrupteur `dispatch_paused`.

Restauration isolée (base, artefacts, refs secrets) ; clés et state vérifiés séparément ;
démarrage avec envois suspendus ; chronométrage ; jobs d'écriture en file non réclamés.
Niveau de preuve : testé localement (pas de VM QEMU / lot 3 ; state distant : L2).
"""

from __future__ import annotations

import pytest

import flows
from rackloom import artifacts, recovery, secrets, security
from rackloom.config import get_settings
from rackloom.errors import ApiError
from rackloom.executions import queue

API = "/api/v1"


def test_f26_dispatch_pause_bloque_claim_write(conn, demo, worker_env):
    """Un job d'écriture en file n'est pas réclamé tant que dispatch_paused est actif."""
    project_id = demo.project("vitrine")["id"]
    write_job = queue.enqueue(
        conn,
        kind="tf_apply",
        project_id=project_id,
        idempotency_key="f26-write-paused",
        payload={"probe": True},
    )
    conn.commit()
    assert write_job["state"] == "queued" and write_job["class"] == "write"

    recovery.pause(conn, reason="exercice F26 : suspension des envois", actor="console")
    conn.commit()

    asserted = queue.claim(conn, "test-f26-writer", 30, kinds=["tf_apply"])
    assert asserted is None
    job = flows.job(conn, write_job["id"])
    assert job["state"] == "queued"
    assert job["wait_reason"] == recovery.WAIT_DISPATCH_PAUSED
    assert job["dispatched_at"] is None

    # Les lectures restent réclamables (diagnostic) pendant la suspension.
    inv = queue.enqueue(
        conn,
        kind="inventory_refresh",
        project_id=project_id,
        idempotency_key="f26-inv-read",
        payload={},
    )
    conn.commit()
    leased = queue.claim(conn, "test-f26-reader", 30, kinds=["inventory_refresh"])
    assert leased is not None and leased["id"] == inv["id"]
    assert leased["kind"] == "inventory_refresh"
    conn.rollback()

    recovery.resume(conn, reason="réconciliation faite, reprise des envois", actor="console")
    conn.commit()
    job = flows.job(conn, write_job["id"])
    assert job["wait_reason"] is None
    claimed = queue.claim(conn, "test-f26-writer", 30, kinds=["tf_apply"])
    assert claimed is not None and claimed["id"] == write_job["id"]
    conn.rollback()


def test_f26_exercise_isole_chronometre_et_cles_state_separes(conn, demo, worker_env, tmp_path):
    """Backup + exercice isolé : phases ok, durée mesurée, dispatch resté suspendu."""
    settings = get_settings()
    # Artefact et secret de démo pour le paquet.
    project_id = demo.project("vitrine")["id"]
    artifacts.store(
        conn,
        project_id=project_id,
        change_id=None,
        kind="f26-probe",
        key_class="B",
        plaintext=b"rapport-filtre-f26",
    )
    secrets.put(conn, "secret://f26/probe", b"valeur-de-test-f26", created_by=demo.principal("admin").user_id)
    # State local fictif (procédure séparée).
    ws = settings.workspaces_dir / "stack-f26"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "terraform.tfstate").write_text('{"version": 4, "serial": 1, "lineage": "f26-test"}', encoding="utf-8")
    conn.commit()

    backup = tmp_path / "backup"
    isolated = tmp_path / "isolated"
    result = recovery.create_backup(conn, out_dir=backup)
    assert result["artifacts"] >= 1
    assert result["secret_refs"] >= 1
    assert result["keys_complete"] is True
    assert (backup / "manifest.json").exists()
    assert (backup / "keys-separate" / "keys-checklist.json").exists()
    assert (backup / "state-separate" / "state-manifest.json").exists()
    # Les clés ne sont pas dans le dossier database.
    assert not (backup / "database" / "secrets-box.key").exists()

    exercise = recovery.run_exercise(
        conn,
        backup_dir=backup,
        isolated_root=isolated,
        reason="exercice périodique de restauration F26",
        actor="console",
    )
    conn.commit()
    assert exercise["status"] == "passed"
    assert exercise["dispatch_paused"] is True
    assert exercise["duration_ms"] is not None and exercise["duration_ms"] >= 0
    assert exercise["db_status"] == "ok"
    assert exercise["artifacts_status"] == "ok"
    assert exercise["secret_refs_status"] == "ok"
    assert exercise["keys_status"] == "ok"
    assert exercise["state_status"] == "simulated"
    assert exercise["simulated_state"] is True
    assert (isolated / "database" / "summary.json").exists()
    assert (isolated / "artifacts").exists()
    assert (isolated / "secret_refs.json").exists()
    assert recovery.is_paused(conn) is True

    # Reprise explicite après l'exercice.
    recovery.resume(conn, reason="exercice F26 terminé, reprise", actor="console")
    conn.commit()
    assert recovery.is_paused(conn) is False


def test_f26_cles_absentes_echec_separe(conn, demo, worker_env, tmp_path):
    backup = tmp_path / "backup"
    recovery.create_backup(conn, out_dir=backup)
    # Retire les clés du dossier séparé pour forcer l'échec de la procédure distincte.
    for name in security.KEY_FILES.values():
        path = backup / "keys-separate" / name
        if path.exists():
            path.unlink()
    (backup / "keys-separate" / "keys-checklist.json").unlink(missing_ok=True)
    isolated = tmp_path / "isolated"
    exercise = recovery.run_exercise(
        conn,
        backup_dir=backup,
        isolated_root=isolated,
        reason="exercice F26 clés manquantes",
        actor="console",
        keys_dir=backup / "keys-separate",
    )
    conn.commit()
    assert exercise["status"] == "failed"
    assert exercise["keys_status"] == "failed"
    assert exercise["failure_code"] == "keys_missing"
    # Le paquet principal a pu être restauré avant l'échec des clés.
    assert exercise["db_status"] == "ok"
    assert recovery.is_paused(conn) is True
    recovery.resume(conn, reason="nettoyage après échec", actor="console")
    conn.commit()


def test_f26_api_admin_dispatch_et_exercice(conn, demo, worker_env, api, tmp_path):
    admin = api("admin")
    st = admin.get(f"{API}/platform/status")
    assert st.status_code == 200
    assert st.json()["dispatch"]["paused"] is False

    paused = admin.post(f"{API}/admin/dispatch/pause", json={"reason": "maintenance planifiée F26"})
    assert paused.status_code == 200, paused.text
    assert paused.json()["paused"] is True

    # Opérateur : 403
    forbid = api("alice").post(f"{API}/admin/dispatch/pause", json={"reason": "tentative"})
    assert forbid.status_code == 403

    backup = tmp_path / "backup-api"
    recovery.create_backup(conn, out_dir=backup)
    conn.commit()
    isolated = tmp_path / "isolated-api"
    # Reprendre pour pouvoir lancer l'exercice (qui re-suspend).
    admin.post(f"{API}/admin/dispatch/resume", json={"reason": "fin maintenance avant exercice"})
    ex = admin.post(
        f"{API}/admin/restore-exercises",
        json={
            "reason": "exercice API F26",
            "backup_dir": str(backup),
            "isolated_root": str(isolated),
        },
    )
    assert ex.status_code == 200, ex.text
    body = ex.json()
    assert body["status"] == "passed"
    assert body["duration_ms"] is not None
    listed = admin.get(f"{API}/admin/restore-exercises")
    assert listed.status_code == 200
    assert any(e["id"] == body["id"] for e in listed.json()["exercises"])
    one = admin.get(f"{API}/admin/restore-exercises/{body['id']}")
    assert one.status_code == 200
    assert one.json()["id"] == body["id"]

    # Toujours suspendu après exercice.
    assert admin.get(f"{API}/admin/dispatch").json()["paused"] is True
    admin.post(f"{API}/admin/dispatch/resume", json={"reason": "fin exercice API"})


def test_f26_resume_sans_pause_refuse(conn):
    with pytest.raises(ApiError) as exc:
        recovery.resume(conn, reason="rien à reprendre ici", actor="console")
    assert exc.value.code == "dispatch_not_paused"


def test_f26_verify_keys_et_state_helpers(tmp_path):
    keys = tmp_path / "keys"
    security.generate_keys(keys)
    ok = recovery.verify_keys(keys)
    assert ok["ok"] is True
    empty = tmp_path / "empty-keys"
    empty.mkdir()
    bad = recovery.verify_keys(empty)
    assert bad["ok"] is False and bad["code"] == "keys_missing"

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "x.tfstate").write_text("{}", encoding="utf-8")
    st = recovery.verify_state(ws)
    assert st["ok"] is True and st["simulated"] is True
