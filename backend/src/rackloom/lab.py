"""Laboratoire local jetable : identités SSH des nœuds, ancres de clés d'hôte, liste des nœuds.

Les clés privées sont générées en mémoire et stockées seulement comme secrets scellés
(`secret://lab/ssh/<identité>`), lisibles par le worker seul ; seules les clés publiques
sont écrites sur disque (dossier `.lab/`, monté en lecture seule dans les nœuds).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import audit, hosts, secrets
from .db import Conn, Jsonb

IDENTITIES = ("rackloom-collect", "rackloom-config")


def generate_identities(conn: Conn, pub_dir: Path) -> dict[str, str]:
    """Nouvelle paire par identité ; remplace les précédentes (rotation)."""
    pub_dir.mkdir(parents=True, exist_ok=True)
    publics: dict[str, str] = {}
    for identity in IDENTITIES:
        key = Ed25519PrivateKey.generate()
        private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                    serialization.NoEncryption())
        public = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                               serialization.PublicFormat.OpenSSH).decode() + f" {identity}@rackloom"
        secrets.put(conn, f"secret://lab/ssh/{identity}", private)
        (pub_dir / f"{identity}.pub").write_text(public + "\n", encoding="utf-8")
        publics[identity] = public
    conn.execute("INSERT INTO platform_settings (key, value) VALUES ('lab_ssh_public_keys', %s) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", (Jsonb(publics),))
    audit.record(conn, actor_type="system", actor_id="lab", action="lab.ssh_identities.rotate", outcome="ok",
                 targets={"identities": list(IDENTITIES)})
    return publics


def identities_present(conn: Conn, pub_dir: Path) -> bool:
    row = conn.execute("SELECT value FROM platform_settings WHERE key = 'lab_ssh_public_keys'").fetchone()
    if not row:
        return False
    for identity in IDENTITIES:
        path = pub_dir / f"{identity}.pub"
        if not path.exists() or path.read_text(encoding="utf-8").strip() != row["value"].get(identity):
            return False
        if not secrets.exists(conn, f"secret://lab/ssh/{identity}"):
            return False
    return True


def import_anchor(conn: Conn, address: str, public_key_line: str) -> dict[str, Any]:
    row = hosts.import_anchor(conn, address, public_key_line, "lab-node")
    audit.record(conn, actor_type="system", actor_id="lab", action="lab.host_anchor.import", outcome="ok",
                 targets={"address": address, "fingerprint": row["fingerprint"]})
    return row


def set_nodes(conn: Conn, nodes: list[dict[str, str]]) -> None:
    conn.execute("INSERT INTO platform_settings (key, value) VALUES ('lab_nodes', %s) ON CONFLICT (key) "
                 "DO UPDATE SET value = EXCLUDED.value, updated_at = now()", (Jsonb(nodes),))
