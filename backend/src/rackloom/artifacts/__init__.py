"""Artefacts chiffrés sur disque (AES-256-GCM).

Classe A (plans binaires, sorties brutes) : clé détenue par le worker seul, jamais téléchargeable.
Classe B (rapports filtrés) : écrits par le worker, lisibles par l'API pour un membre du projet.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID

from .. import security
from ..config import get_settings
from ..db import Conn
from ..ids import uuid7

KEY_NAMES = {"A": "artifact-a", "B": "artifact-b"}


def _key(key_class: str) -> bytes:
    return security.load_key(get_settings().keys_dir, KEY_NAMES[key_class])


def store(conn: Conn, *, project_id: UUID, change_id: UUID | None, kind: str, key_class: str,
          plaintext: bytes) -> dict[str, Any]:
    artifact_id = uuid7()
    rel = Path(str(project_id)) / f"{artifact_id}.bin"
    path = get_settings().artifacts_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = security.encrypt_artifact(_key(key_class), key_class, plaintext, artifact_id.bytes)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(blob)
    tmp.chmod(0o600)
    tmp.replace(path)
    return conn.execute(
        "INSERT INTO artifacts (id, change_id, project_id, kind, path, sha256, size, key_class) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (artifact_id, change_id, project_id, kind, str(rel), hashlib.sha256(plaintext).hexdigest(), len(plaintext),
         key_class),
    ).fetchone()


def read(artifact: dict[str, Any]) -> bytes:
    path = get_settings().artifacts_dir / artifact["path"]
    return security.decrypt_artifact(_key(artifact["key_class"]), path.read_bytes(), artifact["id"].bytes)


def path_of(artifact: dict[str, Any]) -> Path:
    return get_settings().artifacts_dir / artifact["path"]
