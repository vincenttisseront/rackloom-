"""Secrets scellés : l'API chiffre avec la clé publique sans pouvoir relire ; le worker seul déchiffre.

Les secrets ne sont jamais renvoyés par l'API, jamais journalisés, et transmis aux runners
sur l'entrée standard uniquement.
"""

from __future__ import annotations

from uuid import UUID

import psycopg

from .. import security
from ..config import get_settings
from ..ids import uuid7


def _pub() -> bytes:
    return (get_settings().keys_dir / security.KEY_FILES["secrets-box-public"]).read_bytes()


def put(conn: psycopg.Connection, ref: str, plaintext: bytes, created_by: UUID | None = None) -> None:
    if not ref.startswith("secret://"):
        raise ValueError("référence secret:// attendue")
    pub = _pub()
    blob = security.seal(pub, plaintext, ref.encode())
    conn.execute(
        "INSERT INTO secrets (id, ref, ciphertext, key_id, created_by) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (ref) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, key_id = EXCLUDED.key_id, "
        "created_by = EXCLUDED.created_by, created_at = now()",
        (uuid7(), ref, blob, security.key_id_for(pub), created_by),
    )


def reveal(conn: psycopg.Connection, ref: str) -> bytes:
    """Worker uniquement (le rôle API n'a pas le droit de lire `ciphertext`)."""
    row = conn.execute("SELECT ciphertext FROM secrets WHERE ref = %s", (ref,)).fetchone()
    if row is None:
        raise KeyError(ref)
    private = security.load_key(get_settings().keys_dir, "secrets-box-private")
    return security.unseal(private, bytes(row["ciphertext"]), ref.encode())


def exists(conn: psycopg.Connection, ref: str) -> bool:
    return conn.execute("SELECT 1 FROM secrets WHERE ref = %s", (ref,)).fetchone() is not None
