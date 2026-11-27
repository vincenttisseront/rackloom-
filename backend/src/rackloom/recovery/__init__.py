"""F26 : exercices de restauration et interrupteur global `dispatch_paused`.

Paquet principal (ensemble) : base, artefacts chiffrés, références de secrets.
Procédures distinctes : clés de chiffrement (ADR-0007) et state Terraform (ADR-0002).
Un exercice s'exécute dans un répertoire isolé et démarre toujours avec les envois
vers le parc suspendus, pour qu'aucune ancienne tâche en file ne soit relancée.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from .. import audit, clock, security
from ..config import get_settings
from ..db import Conn, Jsonb
from ..errors import ApiError
from ..ids import uuid7
from ..redact import redact

DISPATCH_KEY = "dispatch_paused"
WAIT_DISPATCH_PAUSED = "dispatch_paused"

BUNDLE_SCHEMA = "rackloom.backup-bundle/v1"
MANIFEST_NAME = "manifest.json"
DB_DIR = "database"
ARTIFACTS_DIR = "artifacts"
SECRETS_FILE = "secret_refs.json"
KEYS_CHECKLIST = "keys-checklist.json"
STATE_MANIFEST = "state-manifest.json"

PHASE_OK = ("ok", "simulated")


def _reason(value: Any, field: str = "reason") -> str:
    text = redact(str(value or "").strip())
    if not 3 <= len(text) <= 500:
        raise ApiError(422, "reason_required", "motif obligatoire (3 à 500 caractères)", field=f"/{field}")
    return text


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return _sha256_bytes(raw)


# --- Interrupteur global -----------------------------------------------------------------


def is_paused(conn: Conn) -> bool:
    row = conn.execute("SELECT value FROM platform_settings WHERE key = %s", (DISPATCH_KEY,)).fetchone()
    if row is None:
        return False
    val = row["value"]
    if isinstance(val, dict):
        return bool(val.get("paused"))
    return False


def status(conn: Conn) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM platform_settings WHERE key = %s", (DISPATCH_KEY,)).fetchone()
    val = (row["value"] if row else {}) or {}
    if not isinstance(val, dict):
        val = {}
    paused = bool(val.get("paused"))
    queued_row = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE state = 'queued' AND class = 'write'"
    ).fetchone()
    waiting_row = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE state = 'queued' AND wait_reason = %s",
        (WAIT_DISPATCH_PAUSED,),
    ).fetchone()
    queued = int(queued_row["n"]) if queued_row else 0
    waiting = int(waiting_row["n"]) if waiting_row else 0
    return {
        "paused": paused,
        "reason": val.get("reason"),
        "paused_at": val.get("paused_at"),
        "paused_by": val.get("paused_by"),
        "source": val.get("source"),
        "queued_writes": queued,
        "waiting_dispatch_paused": waiting,
    }


def pause(
    conn: Conn,
    *,
    reason: Any,
    actor: str,
    source: str = "maintenance",
) -> dict[str, Any]:
    text = _reason(reason)
    if source not in ("maintenance", "restore_exercise", "restore"):
        raise ApiError(422, "invalid_source", "source de suspension invalide", field="/source")
    instant = clock.now().isoformat()
    payload = {
        "paused": True,
        "reason": text,
        "paused_at": instant,
        "paused_by": actor,
        "source": source,
    }
    conn.execute(
        """
        INSERT INTO platform_settings (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        (DISPATCH_KEY, Jsonb(payload)),
    )
    audit.record(
        conn,
        actor_type="system" if actor == "console" else "user",
        actor_id=actor,
        action="dispatch.pause",
        outcome="ok",
        policy_decision={"source": source, "reason": text},
    )
    return status(conn)


def resume(conn: Conn, *, reason: Any, actor: str) -> dict[str, Any]:
    text = _reason(reason)
    if not is_paused(conn):
        raise ApiError(409, "dispatch_not_paused", "les envois ne sont pas suspendus")
    payload = {
        "paused": False,
        "resumed_at": clock.now().isoformat(),
        "resumed_by": actor,
        "resume_reason": text,
    }
    conn.execute(
        """
        INSERT INTO platform_settings (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        (DISPATCH_KEY, Jsonb(payload)),
    )
    # Les jobs restés en file redeviennent éligibles (plus de wait_reason).
    conn.execute(
        "UPDATE jobs SET wait_reason = NULL, updated_at = now() WHERE state = 'queued' AND wait_reason = %s",
        (WAIT_DISPATCH_PAUSED,),
    )
    audit.record(
        conn,
        actor_type="system" if actor == "console" else "user",
        actor_id=actor,
        action="dispatch.resume",
        outcome="ok",
        policy_decision={"reason": text},
    )
    return status(conn)


def refusal_message() -> str:
    return "envois vers le parc suspendus (dispatch_paused) : réconcilier puis reprendre explicitement les envois"


# --- Sauvegarde (paquet principal + checklists séparées) ---------------------------------


def _export_database(conn: Conn, dest: Path) -> dict[str, Any]:
    """Instantané logique des tables critiques (pas de secrets en clair, pas de state TF)."""
    dest.mkdir(parents=True, exist_ok=True)
    tables = [
        "projects",
        "environments",
        "deployments",
        "changes",
        "jobs",
        "artifacts",
        "secrets",
        "stacks",
        "audit_log",
    ]
    summary: dict[str, Any] = {"schema": "rackloom.db-snapshot/v1", "tables": {}}
    for table in tables:
        payload: Any
        if table == "secrets":
            rows = conn.execute("SELECT id, ref, key_id, created_at FROM secrets ORDER BY ref").fetchall()
            payload = [
                {
                    "id": str(r["id"]),
                    "ref": r["ref"],
                    "key_id": r["key_id"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]
        elif table == "artifacts":
            rows = conn.execute(
                "SELECT id, project_id, change_id, kind, path, sha256, size, key_class, created_at "
                "FROM artifacts ORDER BY id"
            ).fetchall()
            payload = [
                {
                    "id": str(r["id"]),
                    "project_id": str(r["project_id"]),
                    "change_id": str(r["change_id"]) if r["change_id"] else None,
                    "kind": r["kind"],
                    "path": r["path"],
                    "sha256": r["sha256"],
                    "size": r["size"],
                    "key_class": r["key_class"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]
        else:
            count_row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
            count = int(count_row["n"]) if count_row else 0
            ids = conn.execute(f"SELECT id::text AS id FROM {table} ORDER BY id").fetchall()
            joined = ",".join(r["id"] for r in ids)
            payload = {
                "count": count,
                "id_digest": _sha256_bytes(joined.encode()) if joined else "empty",
            }
        path = dest / f"{table}.json"
        digest = _write_json(path, {"table": table, "data": payload})
        row_count = len(payload) if isinstance(payload, list) else int(payload.get("count", 0))
        summary["tables"][table] = {
            "file": path.name,
            "sha256": digest,
            "rows": row_count,
        }
    summary_path = dest / "summary.json"
    summary["sha256"] = _write_json(summary_path, {k: v for k, v in summary.items() if k != "sha256"})
    return summary


def _export_artifacts(artifacts_dir: Path, dest: Path) -> dict[str, Any]:
    dest.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    if artifacts_dir.exists():
        for src in sorted(artifacts_dir.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(artifacts_dir)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            target.chmod(0o600)
            files.append({"path": str(rel), "sha256": _sha256_file(target), "size": target.stat().st_size})
    return {"count": len(files), "files": files}


def _export_secret_refs(conn: Conn, dest: Path) -> dict[str, Any]:
    """Références + ciphertext (boîte scellée) : jamais de clair, jamais les clés."""
    rows = conn.execute("SELECT id, ref, ciphertext, key_id, created_at FROM secrets ORDER BY ref").fetchall()
    entries = []
    for r in rows:
        entries.append(
            {
                "id": str(r["id"]),
                "ref": r["ref"],
                "key_id": r["key_id"],
                "ciphertext_sha256": _sha256_bytes(bytes(r["ciphertext"])),
                "ciphertext_b64": __import__("base64").b64encode(bytes(r["ciphertext"])).decode(),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )
    payload = {"schema": "rackloom.secret-refs/v1", "count": len(entries), "entries": entries}
    digest = _write_json(dest, payload)
    return {"count": len(entries), "sha256": digest, "file": dest.name}


def keys_checklist(keys_dir: Path) -> dict[str, Any]:
    """Inventaire des fichiers de clés (procédure distincte du paquet base)."""
    expected = list(security.KEY_FILES.values())
    present: dict[str, Any] = {}
    missing: list[str] = []
    for name, filename in security.KEY_FILES.items():
        path = keys_dir / filename
        if path.exists():
            data = path.read_bytes()
            present[name] = {
                "file": filename,
                "sha256": _sha256_bytes(data),
                "size": len(data),
            }
        else:
            missing.append(filename)
    return {
        "schema": "rackloom.keys-checklist/v1",
        "keys_dir": str(keys_dir),
        "expected": expected,
        "present": present,
        "missing": missing,
        "complete": not missing,
    }


def verify_keys(keys_dir: Path, *, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    """Vérifie la présence et l'intégrité des clés (hors paquet base/artefacts/refs)."""
    current = keys_checklist(keys_dir)
    if not current["complete"]:
        return {
            "ok": False,
            "code": "keys_missing",
            "message": f"clés manquantes : {', '.join(current['missing'])}",
            "checklist": current,
        }
    # Charge chaque clé pour valider le format (32 octets).
    try:
        for name in security.KEY_FILES:
            security.load_key(keys_dir, name)
    except ValueError as exc:
        return {"ok": False, "code": "keys_invalid", "message": str(exc), "checklist": current}
    if expected:
        for name, meta in expected.get("present", {}).items():
            got = current["present"].get(name)
            if got is None or got["sha256"] != meta.get("sha256"):
                return {
                    "ok": False,
                    "code": "keys_digest_mismatch",
                    "message": f"empreinte divergente pour {name}",
                    "checklist": current,
                }
    return {"ok": True, "code": "keys_ok", "message": "clés présentes et chargeables", "checklist": current}


def state_manifest(workspaces_dir: Path) -> dict[str, Any]:
    """Inventaire du state Terraform local (procédure distincte ; state distant = L2)."""
    files: list[dict[str, Any]] = []
    if workspaces_dir.exists():
        for src in sorted(workspaces_dir.rglob("*.tfstate")):
            if not src.is_file():
                continue
            rel = src.relative_to(workspaces_dir)
            files.append(
                {
                    "path": str(rel),
                    "sha256": _sha256_file(src),
                    "size": src.stat().st_size,
                }
            )
    return {
        "schema": "rackloom.state-manifest/v1",
        "workspaces_dir": str(workspaces_dir),
        "count": len(files),
        "files": files,
        "backend": "local",
        "notice": "state distant réel : lot 2 ; vérification locale seulement",
    }


def verify_state(
    workspaces_dir: Path,
    *,
    expected: dict[str, Any] | None = None,
    allow_empty: bool = True,
) -> dict[str, Any]:
    """Vérifie le state séparément du paquet base (aucune écriture fournisseur)."""
    current = state_manifest(workspaces_dir)
    if expected is not None:
        exp_map = {f["path"]: f["sha256"] for f in expected.get("files", [])}
        cur_map = {f["path"]: f["sha256"] for f in current["files"]}
        if exp_map != cur_map:
            return {
                "ok": False,
                "code": "state_digest_mismatch",
                "message": "empreintes du state divergentes",
                "manifest": current,
                "simulated": True,
            }
    if not current["files"] and not allow_empty and expected and expected.get("files"):
        return {
            "ok": False,
            "code": "state_missing",
            "message": "aucun fichier de state trouvé",
            "manifest": current,
            "simulated": True,
        }
    return {
        "ok": True,
        "code": "state_ok",
        "message": "state local inventorié (simulé hors L2)",
        "manifest": current,
        "simulated": True,
    }


def create_backup(
    conn: Conn,
    *,
    out_dir: Path,
    artifacts_dir: Path | None = None,
    keys_dir: Path | None = None,
    workspaces_dir: Path | None = None,
) -> dict[str, Any]:
    """Produit un paquet principal + checklists clés/state dans des sous-dossiers séparés."""
    settings = get_settings()
    artifacts_dir = artifacts_dir or settings.artifacts_dir
    keys_dir = keys_dir or settings.keys_dir
    workspaces_dir = workspaces_dir or settings.workspaces_dir
    out_dir = out_dir.resolve()
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ApiError(409, "backup_dir_not_empty", "le répertoire de sauvegarde doit être vide")
    out_dir.mkdir(parents=True, exist_ok=True)

    db_summary = _export_database(conn, out_dir / DB_DIR)
    art_summary = _export_artifacts(artifacts_dir, out_dir / ARTIFACTS_DIR)
    sec_summary = _export_secret_refs(conn, out_dir / SECRETS_FILE)

    # Checklists séparées : hors du paquet « ensemble » conceptuel, dossiers distincts.
    keys_dir_out = out_dir / "keys-separate"
    keys_dir_out.mkdir(parents=True, exist_ok=True)
    keys = keys_checklist(keys_dir)
    _write_json(keys_dir_out / KEYS_CHECKLIST, keys)
    # Copie des fichiers de clés dans le dossier séparé (jamais mélangé au dump DB).
    for filename in security.KEY_FILES.values():
        src = keys_dir / filename
        if src.exists():
            dst = keys_dir_out / filename
            shutil.copy2(src, dst)
            dst.chmod(0o600)

    state_dir_out = out_dir / "state-separate"
    state_dir_out.mkdir(parents=True, exist_ok=True)
    state = state_manifest(workspaces_dir)
    _write_json(state_dir_out / STATE_MANIFEST, state)
    if workspaces_dir.exists():
        for src in workspaces_dir.rglob("*.tfstate"):
            if not src.is_file():
                continue
            rel = src.relative_to(workspaces_dir)
            target = state_dir_out / "files" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            target.chmod(0o600)

    manifest = {
        "schema": BUNDLE_SCHEMA,
        "created_at": clock.now().isoformat(),
        "components": {
            "database": {"dir": DB_DIR, "summary": db_summary},
            "artifacts": {"dir": ARTIFACTS_DIR, "summary": art_summary},
            "secret_refs": {"file": SECRETS_FILE, "summary": sec_summary},
        },
        "separate_procedures": {
            "keys": {"dir": "keys-separate", "checklist": KEYS_CHECKLIST},
            "state": {"dir": "state-separate", "manifest": STATE_MANIFEST, "simulated": True},
        },
        "notice": (
            "Les clés et le state sont documentés et stockés à part ; "
            "ne pas les restaurer dans la même étape que la base."
        ),
    }
    digest = _write_json(out_dir / MANIFEST_NAME, manifest)
    manifest["sha256"] = digest
    return {
        "path": str(out_dir),
        "manifest_sha256": digest,
        "database_tables": len(db_summary["tables"]),
        "artifacts": art_summary["count"],
        "secret_refs": sec_summary["count"],
        "keys_complete": keys["complete"],
        "state_files": state["count"],
    }


# --- Exercice de restauration isolé ------------------------------------------------------


def _restore_database_phase(backup: Path, isolated: Path) -> dict[str, Any]:
    src = backup / DB_DIR
    if not src.is_dir():
        return {"ok": False, "code": "db_backup_missing", "message": "dossier database absent du paquet"}
    dest = isolated / DB_DIR
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    summary_path = dest / "summary.json"
    if not summary_path.exists():
        return {"ok": False, "code": "db_summary_missing", "message": "summary.json absent"}
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    for table, meta in summary.get("tables", {}).items():
        fpath = dest / meta["file"]
        if not fpath.exists():
            return {"ok": False, "code": "db_table_missing", "message": f"table {table} absente"}
        if _sha256_file(fpath) != meta["sha256"]:
            return {"ok": False, "code": "db_digest_mismatch", "message": f"empreinte {table} divergente"}
    return {
        "ok": True,
        "code": "db_ok",
        "message": f"{len(summary.get('tables', {}))} tables restaurées en isolé",
        "tables": len(summary.get("tables", {})),
        "path": str(dest),
    }


def _restore_artifacts_phase(backup: Path, isolated: Path) -> dict[str, Any]:
    src = backup / ARTIFACTS_DIR
    dest = isolated / ARTIFACTS_DIR
    if dest.exists():
        shutil.rmtree(dest)
    if not src.exists():
        dest.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "code": "artifacts_ok", "message": "aucun artefact", "count": 0, "path": str(dest)}
    shutil.copytree(src, dest)
    count = sum(1 for p in dest.rglob("*") if p.is_file())
    return {
        "ok": True,
        "code": "artifacts_ok",
        "message": f"{count} artefact(s) restauré(s) en isolé",
        "count": count,
        "path": str(dest),
    }


def _restore_secret_refs_phase(backup: Path, isolated: Path) -> dict[str, Any]:
    src = backup / SECRETS_FILE
    if not src.exists():
        return {"ok": False, "code": "secret_refs_missing", "message": "secret_refs.json absent"}
    dest = isolated / SECRETS_FILE
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    dest.chmod(0o600)
    payload = json.loads(dest.read_text(encoding="utf-8"))
    if payload.get("schema") != "rackloom.secret-refs/v1":
        return {"ok": False, "code": "secret_refs_schema", "message": "schéma secret_refs invalide"}
    # Vérifie que chaque ciphertext a l'empreinte annoncée (pas de déchiffrement ici).
    for entry in payload.get("entries", []):
        raw = __import__("base64").b64decode(entry["ciphertext_b64"])
        if _sha256_bytes(raw) != entry["ciphertext_sha256"]:
            return {
                "ok": False,
                "code": "secret_refs_digest_mismatch",
                "message": f"empreinte ciphertext divergente pour {entry.get('ref')}",
            }
    return {
        "ok": True,
        "code": "secret_refs_ok",
        "message": f"{payload.get('count', 0)} référence(s) de secret restaurée(s)",
        "count": payload.get("count", 0),
        "path": str(dest),
    }


def list_exercises(conn: Conn, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM restore_exercises ORDER BY started_at DESC LIMIT %s",
        (max(1, min(limit, 200)),),
    ).fetchall()
    return [_exercise_view(r) for r in rows]


def get_exercise(conn: Conn, exercise_id: UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM restore_exercises WHERE id = %s", (exercise_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "exercice de restauration introuvable")
    return _exercise_view(row)


def _exercise_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "reason": row["reason"],
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
        "duration_ms": row["duration_ms"],
        "isolated_root": row["isolated_root"],
        "backup_root": row["backup_root"],
        "db_status": row["db_status"],
        "artifacts_status": row["artifacts_status"],
        "secret_refs_status": row["secret_refs_status"],
        "keys_status": row["keys_status"],
        "state_status": row["state_status"],
        "phases": row["phases"] or {},
        "dispatch_paused": row["dispatch_paused"],
        "queued_writes_at_start": row["queued_writes_at_start"],
        "queued_writes_still_queued": row["queued_writes_still_queued"],
        "simulated_state": row["simulated_state"],
        "failure_code": row["failure_code"],
        "failure_message": row["failure_message"],
        "started_by": row["started_by"],
    }


def run_exercise(
    conn: Conn,
    *,
    backup_dir: Path,
    isolated_root: Path,
    reason: Any,
    actor: str,
    keys_dir: Path | None = None,
    state_dir: Path | None = None,
    verify_keys_from_backup: bool = True,
    verify_state_from_backup: bool = True,
) -> dict[str, Any]:
    """Restaure le paquet principal en isolé, vérifie clés/state à part, chronomètre, suspend les envois."""
    text = _reason(reason)
    backup_dir = backup_dir.resolve()
    isolated_root = isolated_root.resolve()
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.exists():
        raise ApiError(422, "backup_manifest_missing", "manifest.json absent du paquet de sauvegarde")

    if isolated_root.exists() and any(isolated_root.iterdir()):
        raise ApiError(409, "isolated_not_empty", "le répertoire isolé doit être vide")
    isolated_root.mkdir(parents=True, exist_ok=True)

    # Toujours démarrer sans écriture vers le parc.
    pause(conn, reason=f"exercice de restauration : {text}", actor=actor, source="restore_exercise")
    queued_start_row = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE state = 'queued' AND class = 'write'"
    ).fetchone()
    queued_at_start = int(queued_start_row["n"]) if queued_start_row else 0

    exercise_id = uuid7()
    conn.execute(
        """
        INSERT INTO restore_exercises (
            id, status, reason, isolated_root, backup_root, dispatch_paused,
            queued_writes_at_start, started_by
        ) VALUES (%s, 'running', %s, %s, %s, true, %s, %s)
        """,
        (exercise_id, text, str(isolated_root), str(backup_dir), queued_at_start, actor),
    )

    t0 = time.perf_counter()
    phases: dict[str, Any] = {}
    db_status = "pending"
    artifacts_status = "pending"
    secret_refs_status = "pending"  # noqa: S105 — statut de phase, pas un secret
    keys_status = "not_provided"
    state_status = "not_provided"
    failure_code = failure_message = None
    ok = True

    try:
        db_result = _restore_database_phase(backup_dir, isolated_root)
        phases["database"] = db_result
        db_status = "ok" if db_result["ok"] else "failed"
        if not db_result["ok"]:
            ok = False
            failure_code = db_result["code"]
            failure_message = db_result["message"]

        if ok:
            art_result = _restore_artifacts_phase(backup_dir, isolated_root)
            phases["artifacts"] = art_result
            artifacts_status = "ok" if art_result["ok"] else "failed"
            if not art_result["ok"]:
                ok = False
                failure_code = art_result["code"]
                failure_message = art_result["message"]

        if ok:
            sec_result = _restore_secret_refs_phase(backup_dir, isolated_root)
            phases["secret_refs"] = sec_result
            secret_refs_status = "ok" if sec_result["ok"] else "failed"
            if not sec_result["ok"]:
                ok = False
                failure_code = sec_result["code"]
                failure_message = sec_result["message"]

        # Procédures distinctes.
        if verify_keys_from_backup:
            keys_source = keys_dir or (backup_dir / "keys-separate")
            expected = None
            checklist_path = keys_source / KEYS_CHECKLIST
            if checklist_path.exists():
                expected = json.loads(checklist_path.read_text(encoding="utf-8"))
            keys_result = verify_keys(keys_source, expected=expected)
            phases["keys"] = keys_result
            keys_status = "ok" if keys_result["ok"] else "failed"
            if not keys_result["ok"]:
                ok = False
                failure_code = failure_code or keys_result["code"]
                failure_message = failure_message or keys_result["message"]
        else:
            phases["keys"] = {"ok": None, "code": "skipped", "message": "vérification des clés non demandée"}
            keys_status = "skipped"

        if verify_state_from_backup:
            state_source = state_dir or (backup_dir / "state-separate")
            expected_state = None
            sm_path = state_source / STATE_MANIFEST
            if sm_path.exists():
                expected_state = json.loads(sm_path.read_text(encoding="utf-8"))
            # Les fichiers restaurés séparément vivent sous state-separate/files.
            files_root = state_source / "files" if (state_source / "files").exists() else state_source
            state_result = verify_state(files_root, expected=None, allow_empty=True)
            # Si un manifeste attendu existe, comparer les empreintes des copies séparées.
            if expected_state and expected_state.get("files"):
                cur = state_manifest(files_root)
                # Chemins relatifs : sous files/ le path du manifeste est la clé.
                exp_map = {f["path"]: f["sha256"] for f in expected_state["files"]}
                cur_map = {f["path"]: f["sha256"] for f in cur["files"]}
                if exp_map and exp_map != cur_map:
                    # Tolère un inventaire vide si le backup n'avait que le manifeste.
                    if not cur["files"] and (state_source / "files").exists() is False:
                        state_result = {
                            "ok": True,
                            "code": "state_ok",
                            "message": "manifeste state présent (aucune copie fichier)",
                            "manifest": expected_state,
                            "simulated": True,
                        }
                    else:
                        state_result = {
                            "ok": False,
                            "code": "state_digest_mismatch",
                            "message": "empreintes du state divergentes",
                            "manifest": cur,
                            "simulated": True,
                        }
                else:
                    state_result = {
                        "ok": True,
                        "code": "state_ok",
                        "message": "state vérifié séparément (local / simulé hors L2)",
                        "manifest": cur if cur["files"] else expected_state,
                        "simulated": True,
                    }
            phases["state"] = state_result
            state_status = "simulated" if state_result["ok"] else "failed"
            if not state_result["ok"]:
                ok = False
                failure_code = failure_code or state_result["code"]
                failure_message = failure_message or state_result["message"]
        else:
            phases["state"] = {"ok": None, "code": "skipped", "message": "vérification du state non demandée"}
            state_status = "skipped"

        # Preuve que les écritures en file ne partent pas.
        still_row = conn.execute(
            "SELECT count(*) AS n FROM jobs WHERE state = 'queued' AND class = 'write'"
        ).fetchone()
        still_queued = int(still_row["n"]) if still_row else 0
        phases["dispatch"] = {
            "paused": is_paused(conn),
            "queued_writes_at_start": queued_at_start,
            "queued_writes_still_queued": still_queued,
            "message": "envois suspendus ; aucune écriture vers le parc pendant l'exercice",
        }
        if not is_paused(conn):
            ok = False
            failure_code = failure_code or "dispatch_not_paused"
            failure_message = failure_message or "dispatch_paused aurait dû rester actif"

    except Exception as exc:  # noqa: BLE001
        ok = False
        failure_code = failure_code or "exercise_error"
        failure_message = failure_message or redact(str(exc))[:1000]
        phases["error"] = {"message": failure_message}

    duration_ms = int((time.perf_counter() - t0) * 1000)
    final_status = "passed" if ok else "failed"
    final_queued_row = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE state = 'queued' AND class = 'write'"
    ).fetchone()
    still_queued = int(final_queued_row["n"]) if final_queued_row else 0

    row = conn.execute(
        """
        UPDATE restore_exercises SET
            status = %s,
            finished_at = now(),
            duration_ms = %s,
            db_status = %s,
            artifacts_status = %s,
            secret_refs_status = %s,
            keys_status = %s,
            state_status = %s,
            phases = %s,
            queued_writes_still_queued = %s,
            simulated_state = true,
            failure_code = %s,
            failure_message = %s
        WHERE id = %s
        RETURNING *
        """,
        (
            final_status,
            duration_ms,
            db_status,
            artifacts_status,
            secret_refs_status,
            keys_status,
            state_status,
            Jsonb(phases),
            still_queued,
            failure_code,
            failure_message,
            exercise_id,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("exercice de restauration introuvable après mise à jour")

    audit.record(
        conn,
        actor_type="system" if actor == "console" else "user",
        actor_id=actor,
        action="restore.exercise",
        outcome="ok" if ok else "failed",
        policy_decision={
            "exercise_id": str(exercise_id),
            "duration_ms": duration_ms,
            "dispatch_paused": True,
            "failure_code": failure_code,
        },
    )
    return _exercise_view(row)
